"""Contract for the two alert fields the ladder needs.

    EXIT_PLAN=DYNAMIC_TRAIL
    INTERVAL={{interval}}

Wei's shape: the alert names a plan, and the numbers behind it live in config
so they can be changed without editing the strategy. The interval has to come
from the alert rather than a configured default — "previous completed bar low"
has no meaning without a bar size, and a default would silently trail a 1h
signal on 1m bars, a stop several times tighter than the strategy was tested
with. That failure is invisible: the orders all look legitimate.

`INTERVAL` is also the field `EVENT_ID={{ticker}}-{{interval}}-{{time}}` needs,
so one alert edit buys both identity and the trail's bar size.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tv_alpaca_gateway import exit_plans
from tv_alpaca_gateway.pine_alert_parser import AlertParseError, parse_pine_alert

BASE = ("EXECUTE_ALPACA_ORDER | SYMBOL=BTC/USD | SIDE=BUY | QTY=0.0015 | "
        "ORDER_TYPE=MARKET | TIME_IN_FORCE=GTC")


def _alert(**fields):
    extra = "".join(f" | {k}={v}" for k, v in fields.items() if v is not None)
    return BASE + extra


# ══════════════════════════════════════════════════════════════ the fields

def test_an_alert_can_name_an_exit_plan():
    command = parse_pine_alert(_alert(EXIT_PLAN="DYNAMIC_TRAIL", INTERVAL="1"))
    assert command.exit_plan == "DYNAMIC_TRAIL"
    assert command.interval == "1"


def test_an_alert_without_an_exit_plan_still_parses():
    """Every alert fired so far has none. Making the field required would stop
    the existing strategy dead at the first firing after deploy."""
    assert parse_pine_alert(BASE).exit_plan is None


def test_an_exit_plan_without_an_interval_is_refused():
    """Not defaulted. A configured default would trail a 1h signal on 1m bars
    and every order would still look legitimate."""
    with pytest.raises(AlertParseError, match=r"(?i)INTERVAL"):
        parse_pine_alert(_alert(EXIT_PLAN="DYNAMIC_TRAIL"))


@pytest.mark.parametrize("interval", ["1", "5", "15", "60", "1H", "D", "W"])
def test_the_intervals_tradingview_actually_renders_are_accepted(interval):
    """TradingView writes minutes as a bare number and coarser frames with a
    suffix. Nobody has captured a real payload yet, so both forms are accepted
    rather than one being guessed at."""
    assert parse_pine_alert(
        _alert(EXIT_PLAN="DYNAMIC_TRAIL", INTERVAL=interval)).interval == interval


@pytest.mark.parametrize("interval", ["one", "1 min", "-5", "1..5", "@"])
def test_an_uninterpretable_interval_is_refused_not_ignored(interval):
    """Treating an unreadable interval as "no interval" is how the freshness
    check quietly stopped existing on the Pine path. Same mistake, same file."""
    with pytest.raises(AlertParseError, match=r"(?i)INTERVAL"):
        parse_pine_alert(_alert(EXIT_PLAN="DYNAMIC_TRAIL", INTERVAL=interval))


# ═══════════════════════════════════════════════════════════ the config

def test_the_named_plan_matches_what_wei_specified():
    plan = exit_plans.resolve("DYNAMIC_TRAIL")
    assert plan.tranches == ((Decimal("0.20"), Decimal("1.2")),
                             (Decimal("0.30"), Decimal("2.5")))
    assert plan.runner_fraction == Decimal("0.50")
    assert plan.breakeven_after == 1
    assert plan.trail_source == "previous_completed_bar_low"


def test_an_unknown_plan_name_is_refused_rather_than_defaulted():
    """An alert naming a plan that does not exist must not quietly become an
    unmanaged position — or, worse, silently get somebody else's ladder."""
    with pytest.raises(KeyError, match=r"(?i)NOPE|unknown"):
        exit_plans.resolve("NOPE")


def test_the_shipped_plans_are_all_internally_consistent():
    """Fractions summing to anything but 1 leaves part of the position with no
    rung and no runner — managed in name only. Checked for every plan, so a
    later edit cannot ship a broken one."""
    for name in exit_plans.names():
        exit_plans.resolve(name).validate()


def test_resolving_a_plan_twice_gives_independent_objects():
    """Belt and braces, and worth being precise about why.

    `ExitPlan` is frozen, so sharing one instance is safe today — this is not
    fixing a live bug. It holds the property in place for the day someone
    unfreezes it, at which point two lots sharing a plan would let a later one
    retroactively re-price a position already in the market.
    """
    first, second = exit_plans.resolve("DYNAMIC_TRAIL"), exit_plans.resolve("DYNAMIC_TRAIL")
    assert first == second
    assert first is not second


# ═══════════════════════════════════════════════════ the OCO-shaped plan

def test_oco_after_fill_is_one_target_for_the_whole_position():
    """A take-profit and a stop, whichever comes first."""
    plan = exit_plans.resolve("OCO_AFTER_FILL")
    assert len(plan.tranches) == 1
    assert plan.tranches[0][0] == Decimal("1.00"), "it should exit the whole position"
    assert plan.runner_fraction == Decimal("0"), "an OCO leaves no runner"
    plan.validate()


def test_a_plan_with_no_runner_needs_no_trail_source():
    """Demanding one would force every take-profit-and-stop plan to name a
    mechanism it never uses."""
    exit_plans.resolve("OCO_AFTER_FILL").validate()


def test_a_plan_with_a_runner_still_requires_a_real_trail_source():
    """The relaxation above must not weaken the plan that does trail."""
    from tv_alpaca_gateway.exit_manager import ExitPlan, ExitPlanError

    with pytest.raises(ExitPlanError, match=r"(?i)trail"):
        ExitPlan(name="X", tranches=((Decimal("0.5"), Decimal("1")),),
                 runner_fraction=Decimal("0.5"), trail_source="none",
                 breakeven_after=1).validate()


def test_an_oco_lot_closes_when_its_single_target_fills():
    """No runner means the position is gone, so the lot must not linger and
    hold the symbol against the one-lot rule."""
    from decimal import Decimal as D

    from tv_alpaca_gateway import exit_manager as m

    from tv_alpaca_gateway.broker import FakeBroker

    broker = FakeBroker()
    broker.positions["BTC/USD"] = D("0.0015")
    lot = m.open_lot(m.Lot.opened(
        event_id="e", symbol="BTC/USD", entry_price=D("65000"),
        initial_stop=D("64900"), held_qty=D("0.0015"), timeframe="1m",
        plan=exit_plans.resolve("OCO_AFTER_FILL"),
        min_order_size=D("0.000015437")), broker)
    assert lot.runner_qty() == 0
    lot.on_fill(rung=1, filled_qty=lot.tranche_qty(1), fill_id="f1")
    assert lot.is_closed


def test_using_a_lot_before_it_is_armed_says_so():
    """It used to fail with AttributeError from three frames down, which sends
    whoever hits it looking at the wrong thing entirely."""
    from decimal import Decimal as D

    from tv_alpaca_gateway import exit_manager as m

    lot = m.Lot.opened(event_id="e", symbol="BTC/USD", entry_price=D("65000"),
                       initial_stop=D("64900"), held_qty=D("0.0015"),
                       timeframe="1m", plan=exit_plans.resolve("OCO_AFTER_FILL"),
                       min_order_size=D("0.000015437"))

    with pytest.raises(m.ExitPlanError, match=r"(?i)not armed|open_lot"):
        lot.on_fill(rung=1, filled_qty=lot.tranche_qty(1), fill_id="f")
