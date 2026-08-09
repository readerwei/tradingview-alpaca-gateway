"""Contract for the wiring — that the ladder is actually reached at runtime.

Everything else in this feature is now green, and none of it proves the code
runs. That was the whole shape of the last gap: `exit_manager` was merged,
tested and unreachable, so an alert with an exit plan produced the old
behaviour and looked entirely successful.

So these tests do not check that the supervisor works. They check that the
gateway *calls* it — that a bar arriving on the socket reaches a lot, that a
restart re-arms before serving, and that the timer exists. A component nobody
invokes fails silently, which is the only failure mode with no symptom.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from tv_alpaca_gateway import exit_manager
from tv_alpaca_gateway.app import create_app
from tv_alpaca_gateway.config import Settings
from tv_alpaca_gateway.store import EventStore
from tv_alpaca_gateway.stream import MarketBar, MarketTrade

ENTRY = Decimal("64960.58")
STOP = Decimal("64100")
HELD = Decimal("0.00149625")
R = ENTRY - STOP

PLAN = dict(name="DYNAMIC_TRAIL",
            tranches=((Decimal("0.20"), Decimal("1.2")), (Decimal("0.30"), Decimal("2.5"))),
            runner_fraction=Decimal("0.50"),
            trail_source="previous_completed_bar_low", breakeven_after=1)


class _Broker:
    def __init__(self, position=HELD):
        self.submitted, self.cancelled = [], []
        self.position = position
        self._resting: dict[str, Decimal] = {}

    def submit_order(self, **kw):
        order_id = f"ord-{len(self.submitted) + 1}"
        self.submitted.append({**kw, "id": order_id})
        if kw.get("type") == "market":
            self.position -= Decimal(str(kw["qty"]))
            return {"id": order_id, "status": "filled", "filled_qty": str(kw["qty"])}
        self._resting[order_id] = Decimal(str(kw["qty"]))
        return {"id": order_id, "status": "new", "filled_qty": "0"}

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        self._resting.pop(order_id, None)

    def position_qty(self, symbol):
        return self.position

    def open_orders(self, symbol):
        return [o for o in self.submitted if o["id"] in self._resting]

    def get_order_by_client_id(self, cid):
        return None

    def min_order_size(self, symbol):
        return Decimal("0.000015437")

    def fill_price(self, order_id):
        return ENTRY

    def get_order(self, order_id):
        raise AssertionError("not used here")

    def latest_trade_price(self, symbol):
        return float(ENTRY)


def _settings(tmp_path, **kw):
    base = dict(paper_trading=True, trading_enabled=True, webhook_secret="s",
                allowed_symbols=frozenset({"BTC/USD"}), max_qty=3,
                crypto_max_qty=Decimal("0.05"), max_notional=3500.0,
                db_path=tmp_path / "wiring.sqlite3")
    base.update(kw)
    return Settings(**base)


def _stored_lot(store, stage="runner", working_stop=ENTRY):
    lot = exit_manager.Lot.opened(
        event_id="evt-1", symbol="BTC/USD", entry_price=ENTRY, initial_stop=STOP,
        held_qty=HELD, timeframe="1m", plan=exit_manager.ExitPlan(**PLAN),
        min_order_size=Decimal("0.000015437"))
    lot.stage, lot.working_stop = stage, working_stop
    store.save_lot(lot.event_id, lot.symbol, lot.stage, exit_manager.dump_lot(lot))
    return lot


# ═══════════════════════════════════════════════ the socket reaches the lot

def _streaming_app(tmp_path, broker):
    """An app with the stream constructed but never started.

    Constructing it is what binds the handlers, and reading them back off the
    socket object is the only way to prove the app passed the ones it claims
    to — asserting on a closure the app never wired would be the same mistake
    one level up.
    """
    settings = _settings(tmp_path, stream_enabled=True,
                         alpaca_key_id="PK-test", alpaca_secret_key="s",
                         crypto_symbols=("BTC/USD",))
    store = EventStore(settings.db_path)
    _stored_lot(store)
    app = create_app(settings, broker, store)
    app.state.supervisor.start()          # re-arm without running lifespan
    return app


def test_a_bar_from_the_socket_moves_a_lots_trail(tmp_path):
    """The handler the stream will actually call, not one built for the test."""
    broker = _Broker()
    app = _streaming_app(tmp_path, broker)
    lot = app.state.supervisor._for("BTC/USD")
    assert lot is not None, "startup did not re-arm the stored lot"

    asyncio.run(app.state.stream.crypto.on_bar(MarketBar(
        symbol="BTC/USD", timestamp="t", open=ENTRY, high=ENTRY + 3 * R,
        low=ENTRY + 2 * R, close=ENTRY + 3 * R, volume=Decimal("1"),
        trade_count=12, raw={})))

    assert lot.working_stop == ENTRY + 2 * R, (
        "a bar arriving on the crypto socket did not reach the lot")


def test_a_quote_only_bar_from_the_socket_is_ignored(tmp_path):
    """End to end this time: the trade_count carried by the parser has to
    survive all the way into the trail decision, not just exist on the dataclass."""
    broker = _Broker()
    app = _streaming_app(tmp_path, broker)
    lot = app.state.supervisor._for("BTC/USD")

    asyncio.run(app.state.stream.crypto.on_bar(MarketBar(
        symbol="BTC/USD", timestamp="t", open=ENTRY, high=ENTRY + 3 * R,
        low=ENTRY + 2 * R, close=ENTRY + 3 * R, volume=Decimal("0"),
        trade_count=0, raw={})))

    assert lot.working_stop == ENTRY, "the trail followed a bar with no trades"


def test_a_trade_from_the_socket_reaches_the_lot(tmp_path):
    broker = _Broker()
    app = _streaming_app(tmp_path, broker)
    lot = app.state.supervisor._for("BTC/USD")
    lot.working_stop = ENTRY + R                     # trail has moved up

    asyncio.run(app.state.stream.crypto.on_trade(MarketTrade(
        symbol="BTC/USD", timestamp="t", price=float(ENTRY), size=1.0,
        trade_id=1, raw={})))

    assert lot.is_closed, "a trade through the trail did not exit the lot"
    assert any(o["type"] == "market" and o["side"] == "sell"
               for o in broker.submitted)


# ═══════════════════════════════════════════════════ startup and the timer

def test_startup_re_arms_stored_lots_before_serving(tmp_path):
    """A lot whose stop vanished while the process was down is unprotected
    right now; the first tick may be a minute away, or never if the one
    market-data connection slot is taken by something else."""
    from fastapi.testclient import TestClient

    settings = _settings(tmp_path)
    store = EventStore(settings.db_path)
    _stored_lot(store, stage="ladder", working_stop=STOP)
    broker = _Broker()

    with TestClient(create_app(settings, broker, store)) as client:
        client.get("/healthz")
        assert broker.submitted, "startup left a live lot with no resting stop"
        assert broker.submitted[-1]["type"] == "stop_limit"


def test_the_reconcile_timer_actually_fires(tmp_path):
    """Asserted by observing a call, not by finding a task.

    The first version of this test fell back to `settings.lot_reconcile_seconds
    > 0` when it could not find the task — which is true by construction and
    proves nothing. A timer that is created and immediately garbage collected
    would have satisfied it, and so would one that never runs.
    """
    import time

    from fastapi.testclient import TestClient

    settings = _settings(tmp_path, lot_reconcile_seconds=0.05)
    store = EventStore(settings.db_path)
    _stored_lot(store)
    app = create_app(settings, _Broker(), store)

    calls = []
    app.state.supervisor.reconcile_all = lambda: calls.append(1)

    with TestClient(app) as client:
        client.get("/healthz")
        time.sleep(0.3)

    assert calls, "the reconcile timer never ran"


def test_the_reconcile_timer_stops_with_the_app(tmp_path):
    """A task left running past shutdown keeps a database handle and a thread
    pool alive, and in tests it leaks across cases."""
    import time

    from fastapi.testclient import TestClient

    settings = _settings(tmp_path, lot_reconcile_seconds=0.05)
    store = EventStore(settings.db_path)
    _stored_lot(store)
    app = create_app(settings, _Broker(), store)
    calls = []
    app.state.supervisor.reconcile_all = lambda: calls.append(1)

    with TestClient(app) as client:
        client.get("/healthz")
        time.sleep(0.2)
    after_shutdown = len(calls)
    time.sleep(0.2)

    assert len(calls) == after_shutdown, "the timer outlived the application"


def test_the_timer_can_be_switched_off(tmp_path):
    """0 disables it, so an operator debugging a runaway reconcile has a lever
    that does not require a code change."""
    from fastapi.testclient import TestClient

    settings = _settings(tmp_path, lot_reconcile_seconds=0)
    with TestClient(create_app(settings, _Broker(), EventStore(settings.db_path))) as client:
        assert client.get("/healthz").status_code == 200


def test_the_supervisor_is_reachable_for_inspection(tmp_path):
    """An operator needs to be able to ask what the gateway thinks it is
    managing without reading the database by hand."""
    settings = _settings(tmp_path)
    app = create_app(settings, _Broker(), EventStore(settings.db_path))
    assert app.state.supervisor is not None


# ═══════════════════════════════════════════════════════════ the handlers

@pytest.mark.parametrize("handler", ["on_bar", "on_trade"])
def test_the_stream_was_given_the_handlers_it_is_supposed_to_have(tmp_path, handler):
    """Guards the guard above: if create_app stopped passing on_bar, the socket
    tests would still pass by calling a handler nobody wired."""
    app = _streaming_app(tmp_path, _Broker())
    assert getattr(app.state.stream.crypto, handler) is not None, (
        f"the crypto socket was constructed without {handler}")
