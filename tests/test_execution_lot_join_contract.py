"""Contract for the seam where a filled entry becomes a managed lot.

This is the join that was missing: everything in #31 was reachable only by
calling it directly, so an alert carrying `EXIT_PLAN=DYNAMIC_TRAIL` would have
produced last night's behaviour — an entry and one plain stop — while looking
entirely successful. A feature that is built, merged, green, and never invoked
is the hardest kind of gap to see, because nothing fails.

Two rules do the work here:

* the ladder's disaster stop **replaces** the ordinary protective order rather
  than joining it. Two resting sells for the same coins is not double
  protection — the second is refused for want of available quantity, and on
  crypto both then block the next entry;
* a ladder that cannot be armed falls back to the ordinary stop. Refusing to
  protect a fill because its ladder was misconfigured would be a worse outcome
  than the misconfiguration.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tv_alpaca_gateway import execution
from tv_alpaca_gateway.config import Settings
from tv_alpaca_gateway.pine_alert_parser import parse_pine_alert
from tv_alpaca_gateway.store import EventStore

ALERT = ("EXECUTE_ALPACA_ORDER | SYMBOL=BTC/USD | SIDE=BUY | QTY=0.0015 | "
         "ORDER_TYPE=MARKET | TIME_IN_FORCE=GTC | "
         "PLACE_PROTECTIVE_STOP_AFTER_FILL | STOP_TRIGGER=64100 | STOP_LIMIT=64000")
FILL = Decimal("64960.58")
HELD = Decimal("0.00149625")


def _settings(tmp_path):
    return Settings(
        paper_trading=True, trading_enabled=True, webhook_secret="s",
        allowed_symbols=frozenset({"BTC/USD"}), max_qty=3,
        crypto_max_qty=Decimal("0.05"), max_notional=3500.0,
        db_path=tmp_path / "join.sqlite3")


class _Broker:
    """Fills the entry, records everything, and enforces reserved quantity."""

    def __init__(self):
        self.submitted, self.cancelled = [], []
        self.position = Decimal("0")
        self._resting: dict[str, Decimal] = {}

    def latest_trade_price(self, symbol):
        return float(FILL)

    def submit_order(self, **kw):
        qty = Decimal(str(kw["qty"]))
        if kw["side"] == "sell" and qty > self.position - sum(self._resting.values()):
            raise RuntimeError(f"insufficient qty available (requested {qty})")
        order_id = f"ord-{len(self.submitted) + 1}"
        self.submitted.append({**kw, "id": order_id})
        if kw.get("type") == "market":
            self.position += (qty if kw["side"] == "buy" else -qty) * Decimal("0.9975")
            return {"id": order_id, "status": "filled", "filled_qty": str(qty)}
        self._resting[order_id] = qty
        return {"id": order_id, "status": "new", "filled_qty": "0"}

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        self._resting.pop(order_id, None)

    def position_qty(self, symbol):
        return self.position

    def fill_price(self, order_id):
        return FILL

    def min_order_size(self, symbol):
        return Decimal("0.000015437")

    def open_orders(self, symbol):
        return [o for o in self.submitted if o["id"] in self._resting]

    def get_order_by_client_id(self, cid):
        return next((o for o in self.submitted if o.get("client_order_id") == cid), None)

    def get_order(self, order_id):
        raise AssertionError("not used on this path")


def _run(tmp_path, alert=ALERT, broker=None, store=None):
    broker = broker or _Broker()
    settings = _settings(tmp_path)
    store = store or EventStore(settings.db_path)
    result = execution.execute_pine_command(
        parse_pine_alert(alert), settings, broker, store, delivery_id="d-1")
    return result, broker, store


def _with_plan(interval="1"):
    return ALERT + f" | EXIT_PLAN=DYNAMIC_TRAIL | INTERVAL={interval}"


# ═════════════════════════════════════════════════════ the plan is honoured

def test_an_alert_with_an_exit_plan_opens_a_managed_lot(tmp_path):
    result, _broker, store = _run(tmp_path, _with_plan())

    assert result.protection_status == "lot_opened"
    assert store.open_lot_for("BTC/USD"), "no lot was persisted for the symbol"


def test_the_ladder_replaces_the_plain_stop_rather_than_adding_to_it(tmp_path):
    """Two resting sells for the same coins is not double protection.

    The second is refused for want of available quantity, and on crypto both
    then block the next entry.
    """
    _result, broker, _store = _run(tmp_path, _with_plan())

    resting = [o for o in broker.submitted if o["side"] == "sell"]
    assert len(resting) == 1, f"expected one resting sell, got {len(resting)}"
    assert resting[0]["client_order_id"].endswith("-protection-0")


def test_the_lot_is_priced_from_the_fill_not_the_signal(tmp_path):
    """R is entry minus stop, and a market order into a fast tape does not fill
    where the alert fired. Sizing off the signal puts every target at the wrong
    distance from the risk actually taken."""
    from tv_alpaca_gateway import exit_manager

    _result, _broker, store = _run(tmp_path, _with_plan())
    _event, _symbol, state = store.open_lots()[0]
    lot = exit_manager.load_lot(state)

    assert lot.entry_price == FILL
    assert lot.risk_per_unit == FILL - Decimal("64100")


def test_an_alert_without_an_exit_plan_behaves_exactly_as_before(tmp_path):
    """Every alert fired so far has none. This path must not change."""
    result, broker, store = _run(tmp_path)

    assert result.protection_status == "submitted"
    assert store.open_lots() == []
    assert [o["type"] for o in broker.submitted] == ["market", "stop_limit"]


# ════════════════════════════════════════════════════ failure stays covered

def test_a_ladder_that_cannot_be_armed_still_gets_a_protective_stop(tmp_path):
    """An entry too small to divide into rungs is a configuration mistake.

    Refusing to protect the fill because of it would be a worse outcome than
    the mistake — so the ordinary stop is placed and the position is covered.
    """
    tiny = ALERT.replace("QTY=0.0015", "QTY=0.00005") + \
        " | EXIT_PLAN=DYNAMIC_TRAIL | INTERVAL=1"
    result, broker, store = _run(tmp_path, tiny)

    assert result.protection_status == "submitted", "the fill was left unprotected"
    assert store.open_lots() == [], "a lot was recorded that could not be armed"
    assert any(o["type"] == "stop_limit" for o in broker.submitted)


def test_an_exit_plan_alert_without_an_interval_never_reaches_the_broker(tmp_path):
    """Refused at the parser, before any order exists."""
    from tv_alpaca_gateway.pine_alert_parser import AlertParseError

    with pytest.raises(AlertParseError, match=r"(?i)INTERVAL"):
        parse_pine_alert(ALERT + " | EXIT_PLAN=DYNAMIC_TRAIL")


# ═══════════════════════════════════════════════════════ one lot at a time

def test_a_second_plan_alert_is_refused_while_a_lot_is_open(tmp_path):
    """Wei: "for sure one lot a time."

    On crypto it is also forced. The open lot's stop is a resting sell, and a
    resting sell makes Alpaca refuse the entry buy at submission with **no
    order record at all** — which is how the last one of these took ten
    messages to diagnose. Refusing here produces a reason instead of silence.
    """
    settings = _settings(tmp_path)
    store = EventStore(settings.db_path)
    broker = _Broker()
    _run(tmp_path, _with_plan(), broker, store)

    with pytest.raises(execution.ExecutionError, match=r"(?i)one lot|already"):
        execution.execute_pine_command(
            parse_pine_alert(_with_plan()), settings, broker, store,
            delivery_id="d-2")


def test_the_refusal_is_recorded_so_it_can_be_audited(tmp_path):
    """A refusal that leaves no trace is indistinguishable from an alert that
    never arrived — both look like a quiet log and an empty database."""
    settings = _settings(tmp_path)
    store = EventStore(settings.db_path)
    broker = _Broker()
    _run(tmp_path, _with_plan(), broker, store)

    with pytest.raises(execution.ExecutionError):
        execution.execute_pine_command(
            parse_pine_alert(_with_plan()), settings, broker, store,
            delivery_id="d-2")

    assert any("lot_already_open" in reason
               for reason, _detail in store.refusals_for("")), "refusal not recorded"


def test_a_refused_second_alert_places_no_order(tmp_path):
    settings = _settings(tmp_path)
    store = EventStore(settings.db_path)
    broker = _Broker()
    _run(tmp_path, _with_plan(), broker, store)
    before = len(broker.submitted)

    with pytest.raises(execution.ExecutionError):
        execution.execute_pine_command(
            parse_pine_alert(_with_plan()), settings, broker, store,
            delivery_id="d-2")

    assert len(broker.submitted) == before, "a refused alert still traded"
