"""The ladder, run in both directions.

Not a parallel set of short tests. The point of the signed direction is that
one body of code serves both ways round, so the interesting check is whether
each RULE holds in both — a rule that only holds one way is a bug in the
abstraction, and a separate short suite would let the two drift until only one
of them was ever exercised.

Alpaca settles the scope: BTC/USD reports `shortable=false, marginable=false`,
so shorts are equities. QQQ and TSLA report `shortable=true` with
`easy_to_borrow=true`.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

manager = pytest.importorskip("tv_alpaca_gateway.exit_manager")

PLAN = dict(name="DYNAMIC_TRAIL",
            tranches=((Decimal("0.20"), Decimal("1.2")), (Decimal("0.30"), Decimal("2.5"))),
            runner_fraction=Decimal("0.50"),
            trail_source="previous_completed_bar_low",
            breakeven_after=1, rungs_on_bar_high=True)

LONG = dict(entry_price=Decimal("100"), initial_stop=Decimal("90"), direction=1)
SHORT = dict(entry_price=Decimal("100"), initial_stop=Decimal("110"), direction=-1)


class _Broker:
    def __init__(self, position=Decimal("100")):
        self.submitted, self.cancelled = [], []
        self._position = position
        self._resting: dict[str, Decimal] = {}

    def submit_order(self, **kw):
        oid = f"o{len(self.submitted) + 1}"
        self.submitted.append({**kw, "id": oid})
        if kw.get("type") == "market":
            return {"id": oid, "status": "filled", "filled_qty": str(kw["qty"])}
        self._resting[oid] = Decimal(str(kw["qty"]))
        return {"id": oid, "status": "new", "filled_qty": "0"}

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        self._resting.pop(oid, None)

    def position_qty(self, _s):
        return self._position

    def open_orders(self, _s):
        return [o for o in self.submitted if o["id"] in self._resting]

    def get_order_by_client_id(self, _c):
        return None

    def min_order_size(self, _s):
        return Decimal("1")


def _lot(**over):
    fields = dict(event_id="e", symbol="QQQ", held_qty=Decimal("100"),
                  timeframe="1m", plan=manager.ExitPlan(**PLAN),
                  min_order_size=Decimal("1"))
    fields.update(over)
    return manager.Lot.opened(**fields)


def _armed(spec, position=Decimal("100")):
    broker = _Broker(position)
    return manager.open_lot(_lot(**spec), broker), broker


BOTH = pytest.mark.parametrize("spec,name", [(LONG, "long"), (SHORT, "short")])


# ══════════════════════════════ each rule, both directions

@BOTH
def test_r_is_positive_either_way(spec, name):
    """R is a distance. If it goes negative on one side every target inverts."""
    assert _lot(**spec).risk_per_unit == Decimal("10"), name


@BOTH
def test_targets_sit_in_the_profitable_direction(spec, name):
    lot = _lot(**spec)
    t1, t2 = lot.target_price(1), lot.target_price(2)

    if name == "long":
        assert t1 == Decimal("112") and t2 == Decimal("125")
    else:
        assert t1 == Decimal("88") and t2 == Decimal("75")
    assert lot.sign * (t2 - t1) > 0, "the later target must be further along"


@BOTH
def test_a_rung_fires_only_when_price_reaches_its_target(spec, name):
    lot, broker = _armed(spec)
    short = name == "short"

    lot.on_price(Decimal("105") if short else Decimal("105"))   # between entry and t1
    assert not [o for o in broker.submitted if o["type"] == "market"], (
        f"{name}: a rung fired before its target")

    lot.on_price(Decimal("85") if short else Decimal("115"))
    fired = [o for o in broker.submitted if o["type"] == "market"]
    assert fired, f"{name}: the rung did not fire at its target"
    assert fired[0]["side"] == ("buy" if short else "sell")


@BOTH
def test_the_stop_is_on_the_losing_side_and_the_right_way_round(spec, name):
    _lot_, broker = _armed(spec)
    stop = [o for o in broker.submitted if o["type"] in ("stop", "stop_limit")][0]

    assert stop["side"] == ("buy" if name == "short" else "sell")
    assert Decimal(str(stop["qty"])) > 0, "quantities to the broker are absolute"


@BOTH
def test_the_stop_is_resized_before_the_tranche_is_exited(spec, name):
    """The ordering the whole design rests on, checked in both directions."""
    lot, broker = _armed(spec)
    lot.on_price(Decimal("85") if name == "short" else Decimal("115"))

    kinds = [(o["type"], Decimal(str(o["qty"]))) for o in broker.submitted]
    assert kinds[0][0] in ("stop", "stop_limit") and kinds[0][1] == Decimal("100")
    assert kinds[1][0] in ("stop", "stop_limit"), f"{name}: sold before resizing"
    assert kinds[1][1] == Decimal("80")
    assert kinds[2][0] == "market" and kinds[2][1] == Decimal("20")


@BOTH
def test_the_trail_only_ever_locks_in_profit(spec, name):
    lot, _broker = _armed(spec)
    lot.advance_to_runner()
    short = name == "short"

    lot.on_bar(high=Decimal("95") if short else Decimal("120"),
               low=Decimal("80") if short else Decimal("105"),
               close=Decimal("90") if short else Decimal("110"), trade_count=5)
    moved = lot.working_stop
    assert lot.sign * (moved - Decimal("110" if short else "90")) > 0, (
        f"{name}: the trail did not move toward profit")

    # a bar that would loosen it must not
    lot.on_bar(high=Decimal("99") if short else Decimal("112"),
               low=Decimal("97") if short else Decimal("100"),
               close=Decimal("98") if short else Decimal("108"), trade_count=5)
    assert lot.working_stop == moved, f"{name}: the trail loosened"


@BOTH
def test_breakeven_moves_the_stop_to_entry(spec, name):
    lot, _broker = _armed(spec)
    lot.on_price(Decimal("85") if name == "short" else Decimal("115"))
    lot.on_fill(rung=1, filled_qty=lot.tranche_qty(1), fill_id="f")

    assert lot.working_stop == Decimal("100"), f"{name}: breakeven not applied"


@BOTH
def test_a_stop_on_the_wrong_side_of_entry_is_refused(spec, name):
    inverted = dict(spec)
    inverted["initial_stop"] = (Decimal("90") if name == "short" else Decimal("110"))

    with pytest.raises(manager.ExitPlanError, match=r"(?i)zero or negative|wrong side"):
        _lot(**inverted)


@BOTH
def test_direction_survives_the_store(spec, name):
    lot = _lot(**spec)
    revived = manager.load_lot(manager.dump_lot(lot))

    assert revived.direction == lot.direction
    assert revived.target_price(1) == lot.target_price(1)
    assert revived.exit_side == lot.exit_side


# ═══════════════════════════ admission: shorts are equities only

def test_a_crypto_short_is_refused_with_alpacas_reason():
    """Measured, not assumed: BTC/USD reports shortable=false and
    marginable=false. The refusal names that rather than our design, because a
    limitation of the exchange and a limitation of our code should not read the
    same to whoever hits it."""
    from tv_alpaca_gateway.pine_alert_parser import AlertParseError, parse_pine_alert

    alert = ("EXECUTE_ALPACA_ORDER | SYMBOL=BTC/USD | SIDE=SELL | QTY=0.0015 | "
             "ORDER_TYPE=MARKET | TIME_IN_FORCE=GTC | EXIT_PLAN=DYNAMIC_TRAIL | "
             "INTERVAL=1 | PLACE_PROTECTIVE_STOP_AFTER_FILL | STOP_TRIGGER=66000 | "
             "STOP_LIMIT=66100")

    with pytest.raises(AlertParseError, match=r"(?i)shortable|cannot be shorted"):
        parse_pine_alert(alert)


def test_an_equity_short_with_a_plan_is_accepted():
    """QQQ and TSLA report shortable=true, easy_to_borrow=true."""
    from tv_alpaca_gateway.pine_alert_parser import parse_pine_alert

    alert = ("EXECUTE_ALPACA_ORDER | SYMBOL=TSLA | SIDE=SELL | QTY=10 | "
             "ORDER_TYPE=MARKET | TIME_IN_FORCE=DAY | EXIT_PLAN=DYNAMIC_TRAIL | "
             "INTERVAL=1 | PLACE_PROTECTIVE_STOP_AFTER_FILL | STOP_TRIGGER=350 | "
             "STOP_LIMIT=350.5")

    command = parse_pine_alert(alert)
    assert command.side == "sell" and command.exit_plan == "DYNAMIC_TRAIL"


def test_execution_gives_a_short_alert_a_short_lot():
    """The wiring. A SIDE=SELL alert must produce direction=-1, or the ladder
    prices every target on the wrong side of the entry."""
    import inspect

    from tv_alpaca_gateway import execution

    source = inspect.getsource(execution._open_managed_lot)
    assert "direction=-1 if command.side == \"sell\" else 1" in source
