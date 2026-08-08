"""Executable specification for Stage 3: turning a parsed Pine command into orders.

WRITTEN BEFORE THE IMPLEMENTATION, ON PURPOSE
---------------------------------------------
Every failure this repository produced tonight was a green check measuring
something other than what shipped: 88 tests passing while two agreed
constraints were absent, a smoke test that called a connected-but-silent socket
a success, a stream suite that monkeypatched away every line touching the wire.
Tests written after the code tend to describe the code. These describe the
contract, so the implementation has to meet them rather than explain them.

WHAT IS ASSERTED HERE IS MEASURED, NOT ASSUMED
-----------------------------------------------
Run against Alpaca paper on 2026-08-07 with real credentials:

    trailing_stop on BTC/USD   -> "invalid order type for crypto order"
    stop        on BTC/USD     -> "invalid order type for crypto order"
    stop_limit  on BTC/USD     -> accepted
    stop_limit, one price only -> "stop limit orders require both stop and limit price"
    trailing_stop on QQQ       -> accepted
    day TIF     on BTC/USD     -> rejected (crypto wants gtc/ioc)
    filled 0.001 BTC           -> position 0.0009975   (fee charged IN KIND)

That last line is the one that will bite. A protective stop sized to the
FILLED quantity is 0.25% larger than the position and Alpaca will refuse it —
leaving an unprotected position and a log line saying protection was placed.

THE INTERFACE
-------------
These tests call `tv_alpaca_gateway.execution.execute_pine_command`. The name
is a proposal, not a requirement: if a different shape suits the
implementation, rename it here and keep the behaviour. What must not change is
what the assertions say.

The fake brokers below double as the specification of the broker interface the
executor may rely on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from tv_alpaca_gateway.config import Settings
from tv_alpaca_gateway.pine_alert_parser import parse_pine_alert
from tv_alpaca_gateway.store import EventStore

execution = pytest.importorskip(
    "tv_alpaca_gateway.execution",
    reason="Stage 3 not implemented yet — see test_stage_three_module_exists")


# ─────────────────────────────────────────────────────────── fakes

@dataclass
class RecordingBroker:
    """Records every call. Fills entries; never talks to a network.

    `position_qty` deliberately returns LESS than the filled quantity, because
    Alpaca charges the crypto fee in kind. An executor that sizes protection
    from the fill will produce a quantity this broker rejects, which is exactly
    what the real one does.
    """

    fill_price: Decimal = Decimal("64890.60")
    fee_rate: Decimal = Decimal("0.0025")
    submitted: list[dict] = field(default_factory=list)
    canceled: list[str] = field(default_factory=list)
    _positions: dict = field(default_factory=dict)

    def latest_trade_price(self, symbol: str) -> float:
        return float(self.fill_price)

    def submit_order(self, **kwargs) -> dict:
        order_type = kwargs.get("type", "market")
        symbol = kwargs["symbol"]
        qty = Decimal(str(kwargs["qty"]))

        if "/" in symbol:                      # crypto restrictions, measured
            if order_type in {"trailing_stop", "stop"}:
                raise RuntimeError("invalid order type for crypto order")
            if kwargs.get("time_in_force") not in {"gtc", "ioc"}:
                raise RuntimeError("invalid time_in_force for crypto order")
        if order_type == "stop_limit" and not (
                kwargs.get("stop_price") and kwargs.get("limit_price")):
            raise RuntimeError("stop limit orders require both stop and limit price")

        held = self._positions.get(symbol, Decimal("0"))
        # ANY sell of more than is held is refused, market included. The real
        # broker does this, and it is what makes "size the flatten from the
        # position" an enforced requirement rather than a weak assertion about
        # a number being small enough.
        if kwargs["side"] == "sell" and qty > held:
            raise RuntimeError(
                f"insufficient balance: requested {qty}, available {held}")

        self.submitted.append(dict(kwargs))
        order_id = f"ord-{len(self.submitted)}"
        if order_type == "market":
            if kwargs["side"] == "buy":
                # The fee is deducted from the asset RECEIVED, so a buy credits
                # less than it filled. Selling returns cash, so the position
                # falls by exactly the quantity sold.
                self._positions[symbol] = held + qty * (1 - self.fee_rate)
            else:
                self._positions[symbol] = held - qty
            return {"id": order_id, "status": "filled", "filled_qty": str(qty)}
        return {"id": order_id, "status": "new", "filled_qty": "0"}

    def position_qty(self, symbol: str) -> Decimal:
        return self._positions.get(symbol, Decimal("0"))

    def cancel_order(self, order_id: str) -> None:
        self.canceled.append(order_id)

    # convenience for assertions
    def orders_of(self, order_type: str) -> list[dict]:
        return [o for o in self.submitted if o.get("type") == order_type]


class UnfilledBroker(RecordingBroker):
    """The entry is accepted but never fills."""

    def submit_order(self, **kwargs) -> dict:
        self.submitted.append(dict(kwargs))
        return {"id": f"ord-{len(self.submitted)}", "status": "new", "filled_qty": "0"}

    def position_qty(self, symbol: str) -> Decimal:
        return Decimal("0")


# ───────────────────────────────────────────────────────── fixtures

def _now() -> str:
    """Generated per call: a hardcoded BAR_TIME passes the freshness rule when
    written and fails it minutes later, which reads as a parser regression."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _crypto_alert(event_id="BTCUSD-1-exec") -> str:
    return ("EXECUTE_ALPACA_ORDER | SYMBOL=BTCUSD | SIDE=BUY | QTY=0.001 | "
            f"EVENT_ID={event_id} | BAR_TIME={_now()} | "
            "ORDER_TYPE=MARKET | TIME_IN_FORCE=GTC | "
            "CANCEL_UNFILLED_AT_DEADLINE=YES | "
            "PLACE_PROTECTIVE_STOP_AFTER_FILL | STOP_TRIGGER=64000 | "
            "STOP_LIMIT=63900 | TRAIL=NONE")


CRYPTO_ALERT = _crypto_alert()


def _settings(tmp_path, **kw):
    base = dict(
        paper_trading=True, trading_enabled=True, webhook_secret="s",
        allowed_symbols=frozenset({"BTC/USD", "QQQ"}),
        crypto_max_qty=Decimal("0.01"), max_qty=10, max_notional=1_000_000.0,
        db_path=tmp_path / "exec.sqlite3",
    )
    base.update(kw)
    return Settings(**base)


def _run(alert, settings, broker, store=None, **kw):
    return execution.execute_pine_command(
        parse_pine_alert(alert), settings, broker,
        store or EventStore(settings.db_path), **kw)


# ══════════════════════════════════════ protection sizing — the expensive one

def test_protection_is_sized_from_the_position_not_the_fill(tmp_path):
    """Alpaca charges the crypto fee IN KIND.

    Measured: a filled 0.001 BTC leaves a position of 0.0009975. Sizing the
    protective stop from `filled_qty` asks to sell more than is held, and the
    broker refuses — so the position ends up unprotected while the logs say a
    stop was placed. The quantity must come from the broker's own position.
    """
    broker = RecordingBroker()
    _run(CRYPTO_ALERT, _settings(tmp_path), broker)

    stops = broker.orders_of("stop_limit")
    assert len(stops) == 1, "no protective stop was submitted"
    assert Decimal(str(stops[0]["qty"])) == broker.position_qty("BTC/USD")
    assert Decimal(str(stops[0]["qty"])) < Decimal("0.001"), (
        "protection was sized from the fill, not the position")


def test_protection_quantity_is_read_from_the_broker_not_computed(tmp_path):
    """`filled_qty * (1 - fee)` drifts; the broker's number does not.

    A broker reporting an unusual position — a partial fill, a pre-existing
    holding, a fee schedule that is not 0.25% — must still produce protection
    matching what is actually held.
    """
    broker = RecordingBroker(fee_rate=Decimal("0.01"))     # not the usual rate
    _run(CRYPTO_ALERT, _settings(tmp_path), broker)

    stop = broker.orders_of("stop_limit")[0]
    assert Decimal(str(stop["qty"])) == broker.position_qty("BTC/USD")


# ═══════════════════════════════════════════ crypto order-type restrictions

def test_crypto_protection_uses_stop_limit_with_both_prices(tmp_path):
    """Measured: plain `stop` is refused for crypto, and `stop_limit` requires
    both a stop and a limit price."""
    broker = RecordingBroker()
    _run(CRYPTO_ALERT, _settings(tmp_path), broker)

    stop = broker.orders_of("stop_limit")[0]
    assert stop["side"] == "sell", "protection for a long must be a sell"
    assert stop["stop_price"] and stop["limit_price"], "both prices are required"
    assert Decimal(str(stop["limit_price"])) <= Decimal(str(stop["stop_price"]))
    assert stop["time_in_force"] in {"gtc", "ioc"}
    assert not broker.orders_of("stop"), "plain stop is invalid for crypto"
    assert not broker.orders_of("trailing_stop"), "trailing_stop is invalid for crypto"


def test_a_crypto_trail_never_reaches_the_broker(tmp_path):
    """The parser already rejects it; the executor must not resurrect it."""
    broker = RecordingBroker()
    with pytest.raises(Exception):
        _run(_crypto_alert().replace("TRAIL=NONE", "TRAIL=250"),
             _settings(tmp_path), broker)
    assert not broker.orders_of("trailing_stop")


# ══════════════════════════════════════════════ fill verification lifecycle

def test_no_protection_is_placed_when_the_entry_does_not_fill(tmp_path):
    """Protection on an unfilled entry would sell a position that is not held.
    The broker refuses it, but the executor must not try."""
    broker = UnfilledBroker()
    _run(CRYPTO_ALERT, _settings(tmp_path), broker)

    assert not broker.orders_of("stop_limit"), "protected a position that does not exist"


def test_the_entry_is_submitted_before_any_protection(tmp_path):
    broker = RecordingBroker()
    _run(CRYPTO_ALERT, _settings(tmp_path), broker)

    types = [o.get("type") for o in broker.submitted]
    assert types[0] == "market", "protection was submitted before the entry"


def test_an_unfilled_entry_is_cancelled_when_the_alert_asks(tmp_path):
    """CANCEL_UNFILLED_AT_DEADLINE=YES is in Wei's live alert.

    Called with the DEFAULT deadline, deliberately. My first version passed
    `deadline_seconds=0`, which is the one value where a `deadline <= 0` guard
    happens to fire — so an implementation that never waits and never cancels
    satisfied the test while leaving the entry working forever. Specifying the
    degenerate path and calling it a contract is the exact failure this file
    exists to prevent, and I committed it here first.

    A short but non-zero deadline is used so the test stays fast while still
    exercising the real branch.
    """
    broker = UnfilledBroker()
    _run(CRYPTO_ALERT, _settings(tmp_path), broker, deadline_seconds=0.05)

    assert broker.canceled, (
        "an unfilled entry with CANCEL_UNFILLED_AT_DEADLINE=YES was left working")


def test_a_filled_entry_is_never_cancelled(tmp_path):
    """The other half: the deadline must not cancel something already filled,
    or the position is closed out from under the strategy."""
    broker = RecordingBroker()
    _run(CRYPTO_ALERT, _settings(tmp_path), broker, deadline_seconds=0.05)

    assert not broker.canceled, "a filled entry was cancelled at the deadline"


# ═════════════════════════════════════════════════════════════ idempotency

def test_the_same_command_does_not_place_two_entries(tmp_path):
    """A relay retry, a duplicate webhook, a re-sent alert — none may double
    the position. This is the failure that costs the most and is easiest to
    introduce while making retries possible."""
    settings = _settings(tmp_path)
    store = EventStore(settings.db_path)
    broker = RecordingBroker()

    _run(CRYPTO_ALERT, settings, broker, store)
    _run(CRYPTO_ALERT, settings, broker, store)

    assert len(broker.orders_of("market")) == 1, "the same alert entered twice"


def test_protection_is_not_duplicated_on_a_repeated_command(tmp_path):
    settings = _settings(tmp_path)
    store = EventStore(settings.db_path)
    broker = RecordingBroker()

    _run(CRYPTO_ALERT, settings, broker, store)
    _run(CRYPTO_ALERT, settings, broker, store)

    assert len(broker.orders_of("stop_limit")) == 1, "two protective stops"


# ════════════════════════════════════════════════════════ the safety gates

def test_the_kill_switch_stops_everything(tmp_path):
    broker = RecordingBroker()
    _run(CRYPTO_ALERT, _settings(tmp_path, trading_enabled=False), broker)
    assert broker.submitted == [], "TRADING_ENABLED=false did not stop the order"


def test_an_unlisted_symbol_never_reaches_the_broker(tmp_path):
    broker = RecordingBroker()
    with pytest.raises(Exception):
        _run(CRYPTO_ALERT, _settings(tmp_path, allowed_symbols=frozenset({"QQQ"})),
             broker)
    assert broker.submitted == []


def test_quantity_is_capped_by_configuration_not_by_the_alert(tmp_path):
    """The alert requests a size; the server bounds it. TradingView asking for
    more than CRYPTO_MAX_QTY must be refused, not silently resized — a silent
    resize makes the fill disagree with the strategy that generated it."""
    broker = RecordingBroker()
    with pytest.raises(Exception):
        _run(_crypto_alert().replace("QTY=0.001", "QTY=5"),
             _settings(tmp_path, crypto_max_qty=Decimal("0.01")), broker)
    assert broker.submitted == []


def test_a_paper_only_configuration_is_still_enforced(tmp_path):
    """Nothing in Stage 3 may weaken the paper-only guarantee."""
    with pytest.raises(ValueError):
        _settings(tmp_path, alpaca_base_url="https://api.alpaca.markets").validate()


# ═════════════════════════════════════ equities are a different asset class

def _equity_alert(event_id="QQQ-1-exec", extra="") -> str:
    return ("EXECUTE_ALPACA_ORDER | SYMBOL=QQQ | SIDE=BUY | QTY=1 | "
            f"EVENT_ID={event_id} | BAR_TIME={_now()} | ORDER_TYPE=MARKET | "
            "TIME_IN_FORCE=DAY | PLACE_PROTECTIVE_STOP_AFTER_FILL | "
            f"STOP_TRIGGER=700 | STOP_LIMIT=699{extra}")


EQUITY_ALERT = _equity_alert()


def test_an_equity_trail_is_allowed_because_alpaca_supports_it(tmp_path):
    """Measured: `trailing_stop` on QQQ is ACCEPTED; on BTC/USD it is refused.

    Tightening crypto must not tighten equities. QQQ is where the 20-EMA
    strategy actually runs, so removing trailing stops there to satisfy a
    crypto restriction would delete the feature from the instrument that needs
    it — the natural over-correction, and the reason this test exists.
    """
    broker = RecordingBroker()
    _run(_equity_alert(extra=" | TRAIL=5"), _settings(tmp_path), broker)

    assert broker.orders_of("market"), "the equity entry was not submitted"


def test_an_equity_entry_keeps_its_day_time_in_force(tmp_path):
    """`day` is invalid for crypto and normal for equities. The asset class
    decides, not a single global rule."""
    broker = RecordingBroker()
    _run(EQUITY_ALERT, _settings(tmp_path), broker)

    entry = broker.orders_of("market")[0]
    assert entry["time_in_force"] == "day"


class _ProtectionFails(RecordingBroker):
    """Refuses the first `fail_times` protective orders, then behaves."""

    def __init__(self, fail_times: int = 99, **kw):
        super().__init__(**kw)
        self.fail_times = fail_times
        self.protection_attempts = 0

    def submit_order(self, **kwargs):
        if kwargs.get("type") in {"stop_limit", "stop", "trailing_stop"}:
            self.protection_attempts += 1
            if self.protection_attempts <= self.fail_times:
                raise RuntimeError("broker refused the protective order")
        return super().submit_order(**kwargs)


def test_a_failed_protective_order_is_retried_once(tmp_path):
    """Wei's call: retry once, then flatten and shout.

    A protective order can fail for reasons that clear immediately — a
    momentary rejection, a position not yet settled on the broker's side. One
    retry costs a round trip; not retrying costs an unprotected position for a
    transient fault.
    """
    broker = _ProtectionFails(fail_times=1)
    _run(CRYPTO_ALERT, _settings(tmp_path), broker)

    assert broker.protection_attempts >= 2, "the protective order was not retried"
    assert broker.orders_of("stop_limit"), "the retry did not place protection"


def test_a_position_that_cannot_be_protected_is_flattened(tmp_path):
    """When protection keeps failing, the position must not be left open.

    An unprotected position is worse than no position: the strategy believes
    its risk is bounded and it is not. Flattening converts an unbounded,
    unattended exposure into a realised loss of known size, which is the
    trade Wei chose.
    """
    broker = _ProtectionFails(fail_times=99)
    _run(CRYPTO_ALERT, _settings(tmp_path), broker)

    closing = [o for o in broker.submitted
               if o.get("type") == "market" and o["side"] == "sell"]
    assert closing, (
        "protection failed repeatedly and the position was left open")
    assert broker.position_qty("BTC/USD") <= 0, "the position was not flattened"


def test_flattening_is_sized_from_the_position_too(tmp_path):
    """The same in-kind fee problem, on the way out.

    A close sized from `filled_qty` asks to sell more than is held. The fake
    refuses it exactly as Alpaca does, so this requirement is enforced by the
    broker's behaviour rather than by an assertion about a number being small
    enough — a flatten sized from the fill cannot reach the assertion at all.
    """
    broker = _ProtectionFails(fail_times=99)
    _run(CRYPTO_ALERT, _settings(tmp_path), broker)

    closing = [o for o in broker.submitted
               if o.get("type") == "market" and o["side"] == "sell"]
    assert closing, "no closing order was submitted"
    assert Decimal(str(closing[0]["qty"])) == Decimal("0.001") * (
        1 - broker.fee_rate), "the close was not sized from the held position"


def test_a_failed_flatten_is_reported_unambiguously(tmp_path):
    """If the position cannot be protected AND cannot be closed, that is the
    worst state the system can reach. It must be impossible to mistake for
    success — silence here is how an operator finds out from a P&L statement.
    """
    class NothingWorks(RecordingBroker):
        def submit_order(self, **kwargs):
            if kwargs.get("type") == "market" and kwargs["side"] == "buy":
                return super().submit_order(**kwargs)
            raise RuntimeError("broker refuses everything else")

    broker = NothingWorks()
    settings = _settings(tmp_path)
    store = EventStore(settings.db_path)

    status = None
    try:
        result = _run(CRYPTO_ALERT, settings, broker, store)
        status = getattr(result, "entry_status", None) or getattr(
            result, "status", None)
    except Exception as exc:
        status = str(exc)

    assert status, "an unprotected, unclosable position produced no signal"
    assert broker.position_qty("BTC/USD") > 0, "fixture: the position is stuck open"


def test_a_failed_protective_order_still_reports_which_order_was_exposed(tmp_path):
    """Whatever happens to the position, the caller must learn the entry id.

    This case originally asserted the position stayed OPEN, which was right
    when it was written and wrong the moment Wei chose retry-then-flatten — two
    cases in this same file then disagreed about the outcome, and an
    implementation could not satisfy both. The durable requirement is not what
    happens to the position; it is that the caller is told which order left
    them exposed, rather than having to find out from a database row.
    """
    class ProtectionRejected(RecordingBroker):
        def submit_order(self, **kwargs):
            if kwargs.get("type") == "stop_limit":
                raise RuntimeError("broker refused the protective order")
            return super().submit_order(**kwargs)

    broker = ProtectionRejected()
    settings = _settings(tmp_path)
    store = EventStore(settings.db_path)

    entry_id = None
    try:
        result = _run(CRYPTO_ALERT, settings, broker, store)
        entry_id = getattr(result, "entry_order_id", None)
    except Exception as exc:
        entry_id = getattr(exc, "entry_order_id", None)

    assert entry_id, (
        "protection failed and the caller was not told which order it was for")


def test_protective_orders_outlive_the_session(tmp_path):
    """A `day` protective stop expires at the close, so an overnight position
    wakes up unprotected while the log still says a stop was placed.

    The alert's TIF describes how the ENTRY should behave. Protection is a
    different order with a different lifetime.
    """
    broker = RecordingBroker()
    _run(EQUITY_ALERT, _settings(tmp_path), broker)

    protection = [o for o in broker.submitted if o.get("type") != "market"][0]
    assert protection["time_in_force"] == "gtc", (
        f"protective stop uses {protection['time_in_force']!r}; it expires at "
        "the close and leaves an overnight position unprotected")


def test_the_broker_order_id_is_recorded_for_every_submission(tmp_path):
    """Without the broker's id there is nothing to reconcile against after a
    restart or a missed stream update — the gap the reconnect resync exists to
    close. An order placed but not recorded is an order that cannot be found."""
    settings = _settings(tmp_path)
    store = EventStore(settings.db_path)
    broker = RecordingBroker()

    result = _run(CRYPTO_ALERT, settings, broker, store)

    assert result is not None, "the executor returned nothing to reconcile with"
    assert getattr(result, "entry_order_id", None) or (
        isinstance(result, dict) and result.get("entry_order_id")), (
        "the entry's broker order id was not returned")


# ═══════════════════ a fill observed while polling is still a fill (regression)

class _FillsDuringPolling(RecordingBroker):
    """Accepted as `new`, filled by the first poll.

    The ordinary case for a marketable order that does not fill instantly, and
    the one the first engine got wrong.
    """

    def submit_order(self, **kwargs):
        result = super().submit_order(**kwargs)
        if kwargs.get("type") == "market" and kwargs["side"] == "buy":
            result["status"] = "new"
        return result

    def get_order(self, order_id):
        return {"id": order_id, "status": "filled", "filled_qty": "0.001"}


def test_an_entry_that_fills_during_polling_is_still_protected(tmp_path):
    """Found by TradingBot in review of the first engine.

    The deadline handler returned as soon as polling observed a fill, so the
    command never reached the protection path: entry accepted as `new`, filled
    a second later, no stop placed. Filled and unprotected — the exact outcome
    the module exists to prevent, reached by the ordinary route rather than an
    exotic one.

    Reproduced before fixing: 0.0009975 held, zero stop orders.
    """
    broker = _FillsDuringPolling()
    result = _run(_crypto_alert(), _settings(tmp_path), broker,
                  deadline_seconds=1.5, poll_interval=0.2)

    assert result.entry_status == "filled"
    assert broker.orders_of("stop_limit"), (
        "the entry filled during polling and no protective stop was placed")
    assert Decimal(str(broker.orders_of("stop_limit")[0]["qty"])) == \
        broker.position_qty("BTC/USD")


def test_an_entry_that_never_fills_is_still_cancelled(tmp_path):
    """The other half: the fix must not turn every unfilled entry into a
    protection attempt on a position that does not exist."""
    broker = UnfilledBroker()
    _run(_crypto_alert(), _settings(tmp_path), broker,
         deadline_seconds=0.4, poll_interval=0.1)

    assert broker.canceled, "an unfilled entry was left working"
    assert not broker.orders_of("stop_limit"), "protected a position that is not held"
