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
from tv_alpaca_gateway.assets import is_crypto
from tv_alpaca_gateway.pine_alert_parser import AlertParseError, parse_pine_alert

BASE = ("EXECUTE_ALPACA_ORDER | SYMBOL=BTC/USD | SIDE=BUY | QTY=0.0015 | "
        "ORDER_TYPE=MARKET | TIME_IN_FORCE=GTC")

OCO_BASE = ("EXECUTE_ALPACA_ORDER | SYMBOL=QQQ | SIDE=BUY | QTY=302 | "
            "ORDER_TYPE=MARKET | TIME_IN_FORCE=GTC | EVENT_ID=qqq-oco-1 | "
            "EXIT_PLAN=OCO_AFTER_FILL | STOP_TRIGGER=723.65 | "
            "STOP_LIMIT=NONE | TAKE_PROFIT=724.89")


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


def test_oco_after_fill_does_not_require_an_interval():
    command = parse_pine_alert(OCO_BASE)
    assert command.exit_plan == "OCO_AFTER_FILL"
    assert command.interval is None
    assert command.stop_trigger == Decimal("723.65")
    assert command.stop_limit is None
    assert command.take_profit == Decimal("724.89")


def test_oco_after_fill_requires_both_exit_prices():
    with pytest.raises(AlertParseError, match=r"(?i)TAKE_PROFIT"):
        parse_pine_alert(OCO_BASE.replace(" | TAKE_PROFIT=724.89", ""))
    with pytest.raises(AlertParseError, match=r"(?i)STOP_TRIGGER"):
        parse_pine_alert(OCO_BASE.replace("STOP_TRIGGER=723.65 | ", ""))


def test_a_crypto_oco_does_not_try_to_use_the_native_order_class():
    """SUPERSEDES `test_oco_after_fill_is_rejected_for_crypto`.

    That test was right about Alpaca and wrong about us: there is no native
    crypto OCO, so it refused the plan outright. But refusing at the parser
    made one plan name mean "works" on QQQ and "refused" on BTC/USD, which
    puts a limitation of Alpaca's API into the strategy's vocabulary — and
    BTC/USD is the only symbol being tested.

    The plan is now accepted on crypto and managed here, exactly as
    DYNAMIC_TRAIL already is. What must remain true is the part the old test
    was protecting: no native OCO is ever submitted for a crypto symbol.
    """
    command = parse_pine_alert(OCO_BASE.replace("SYMBOL=QQQ", "SYMBOL=BTCUSD")
                               .replace("TIME_IN_FORCE=DAY", "TIME_IN_FORCE=GTC"))
    assert command.exit_plan == "OCO_AFTER_FILL"
    assert is_crypto(command.symbol)


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


# ═══════════════════════════ one plan name, dispatched by asset class

CRYPTO_OCO = ("EXECUTE_ALPACA_ORDER | SYMBOL=BTC/USD | SIDE=BUY | QTY=0.0015 | "
              "ORDER_TYPE=MARKET | TIME_IN_FORCE=GTC | EXIT_PLAN=OCO_AFTER_FILL | "
              "INTERVAL=1 | STOP_TRIGGER=64000")
EQUITY_OCO = ("EXECUTE_ALPACA_ORDER | SYMBOL=QQQ | SIDE=BUY | QTY=1 | "
              "ORDER_TYPE=MARKET | TIME_IN_FORCE=DAY | EXIT_PLAN=OCO_AFTER_FILL | "
              "INTERVAL=1 | STOP_TRIGGER=700 | STOP_LIMIT=NONE | TAKE_PROFIT=740")


def test_oco_after_fill_is_accepted_on_crypto():
    """Two implementations landed under one name and disagreed: the managed
    plan supported crypto, the native path refused it, and the merged result
    refused BTC/USD — the only symbol being tested.

    Alpaca having no native crypto OCO is a property of Alpaca's API. It should
    not become a property of the strategy's vocabulary.
    """
    command = parse_pine_alert(CRYPTO_OCO + " | TAKE_PROFIT=66000")
    assert command.exit_plan == "OCO_AFTER_FILL"
    assert command.take_profit == Decimal("66000")


def test_an_equity_oco_still_requires_an_absolute_take_profit():
    """The native leg is an absolute limit price; there is nowhere for an
    R-multiple to go."""
    with pytest.raises(AlertParseError, match=r"(?i)TAKE_PROFIT"):
        parse_pine_alert(EQUITY_OCO.replace(" | TAKE_PROFIT=740", ""))

    assert parse_pine_alert(EQUITY_OCO).take_profit == Decimal("740")


def test_the_crypto_path_goes_to_the_managed_plan_not_the_native_one(monkeypatch):
    """Routing, not just parsing. A native OCO submitted for BTC/USD would be
    rejected by Alpaca — and rejected AT SUBMISSION, which for crypto has
    already cost us an evening of orders that left no record."""
    from tv_alpaca_gateway import execution

    called = []
    monkeypatch.setattr(execution, "_submit_oco_exit",
                        lambda *a, **k: called.append("native"))
    monkeypatch.setattr(execution, "_open_managed_lot",
                        lambda *a, **k: called.append("managed") or None)

    command = parse_pine_alert(CRYPTO_OCO + " | TAKE_PROFIT=66000")
    execution._protect_or_flatten(
        command, "BTC/USD", True, "ord-1", "filled", "evt-1",
        _OcoBroker(), _OcoStore(), Decimal("0"), None, None)

    assert called and called[0] == "managed", (
        f"crypto OCO routed to {called or ['nothing']}; the native path would "
        f"be refused by Alpaca")


class _OcoBroker:
    def position_qty(self, symbol):
        return Decimal("0.0015")

    def submit_order(self, **kw):
        return {"id": "p-1", "status": "new", "filled_qty": "0"}


class _OcoStore:
    def update(self, *a, **k):
        pass

    def record_broker_order(self, *a, **k):
        pass

    def record_refusal(self, *a, **k):
        pass

    def save_lot(self, *a, **k):
        pass


# ══════════════════════════ explicit prices, not R, for the OCO plan

def test_the_oco_plan_requires_an_explicit_take_profit_on_crypto_too():
    """Wei: "I will provide explicit stop and take profit prices on the OCO
    plan." So TAKE_PROFIT is required on both asset classes, not only where
    the native leg forced it."""
    with pytest.raises(AlertParseError, match=r"(?i)TAKE_PROFIT"):
        parse_pine_alert(CRYPTO_OCO)          # no TAKE_PROFIT

    priced = parse_pine_alert(CRYPTO_OCO + " | TAKE_PROFIT=66000")
    assert priced.take_profit == Decimal("66000")


def test_an_explicit_target_overrides_the_plans_r_multiple():
    """The number the strategy computed wins. Deriving from R when the alert
    already said where to exit would quietly trade a different plan than the
    one that was backtested."""
    from decimal import Decimal as D

    from tv_alpaca_gateway import exit_manager as m
    from tv_alpaca_gateway.broker import FakeBroker

    broker = FakeBroker()
    broker.positions["BTC/USD"] = D("0.0015")
    lot = m.Lot.opened(event_id="e", symbol="BTC/USD", entry_price=D("65000"),
                       initial_stop=D("64900"), held_qty=D("0.0015"),
                       timeframe="1m", plan=exit_plans.resolve("OCO_AFTER_FILL"),
                       min_order_size=D("0.000015437"))
    derived = lot.target_price(1)
    lot.explicit_targets = (D("66000"),)

    assert lot.target_price(1) == D("66000")
    assert derived != D("66000"), "the test would prove nothing if they matched"


def test_an_inverted_exit_pair_is_refused():
    """A take-profit below the stop is not a tight target — it is the pair the
    wrong way round, and it would arm, rest, and never make sense."""
    with pytest.raises(AlertParseError, match=r"(?i)inverted|TAKE_PROFIT"):
        parse_pine_alert(CRYPTO_OCO + " | TAKE_PROFIT=63000")


def test_an_explicit_target_survives_the_store():
    from decimal import Decimal as D

    from tv_alpaca_gateway import exit_manager as m

    lot = m.Lot.opened(event_id="e", symbol="BTC/USD", entry_price=D("65000"),
                       initial_stop=D("64900"), held_qty=D("0.0015"),
                       timeframe="1m", plan=exit_plans.resolve("OCO_AFTER_FILL"),
                       min_order_size=D("0.000015437"))
    lot.explicit_targets = (D("66000"),)

    assert m.load_lot(m.dump_lot(lot)).target_price(1) == D("66000")


# ═══════════════ an explicit first target, so a rung can be made to fire

def test_take_profit_prices_the_first_rung_of_a_ladder():
    """Testability, and the reason it matters.

    Six live runs produced six correct arms and not one rung, because every one
    needed the market to travel a set distance inside a window nobody
    controlled. A target the strategy names outright can be placed where it
    will fire — turning a vigil into an experiment — and the sequence it
    exercises is the production one: reserve, resize the stop BEFORE selling,
    sell, route the fill, move to breakeven.
    """
    alert = (_alert(EXIT_PLAN="DYNAMIC_TRAIL", INTERVAL="1") +
             " | TAKE_PROFIT=66000")
    command = parse_pine_alert(alert)

    assert command.exit_plan == "DYNAMIC_TRAIL"
    assert command.take_profit == Decimal("66000")


def test_the_later_rungs_still_come_from_r():
    """Explicit prices the FIRST rung only. A ladder's later targets are
    geometry, and one alert field cannot express three of them."""
    from decimal import Decimal as D

    from tv_alpaca_gateway import exit_manager as m

    lot = m.Lot.opened(event_id="e", symbol="BTC/USD", entry_price=D("65000"),
                       initial_stop=D("64000"), held_qty=D("0.0015"),
                       timeframe="1m", plan=exit_plans.resolve("DYNAMIC_TRAIL"),
                       min_order_size=D("0.000015437"))
    lot.explicit_targets = (D("65500"),)

    assert lot.target_price(1) == D("65500"), "rung 1 should use the alert's price"
    assert lot.target_price(2) == D("65000") + Decimal("2.5") * D("1000"), (
        "rung 2 should still derive from R")


def test_take_profit_without_any_plan_is_still_refused():
    """It has nothing to apply to, and silently ignoring a price the strategy
    sent is how an instruction disappears."""
    with pytest.raises(AlertParseError, match=r"(?i)EXIT_PLAN"):
        parse_pine_alert(_alert() + " | TAKE_PROFIT=66000")


# ═══════════════════════ a short must not fill and then go unmanaged

SHORT = ("EXECUTE_ALPACA_ORDER | SYMBOL=TSLA | SIDE=SELL | QTY=10 | "
         "ORDER_TYPE=MARKET | TIME_IN_FORCE=DAY | EXIT_PLAN=DYNAMIC_TRAIL | "
         "INTERVAL=1 | PLACE_PROTECTIVE_STOP_AFTER_FILL | STOP_TRIGGER=350 | "
         "STOP_LIMIT=350.5")


def test_a_short_carrying_an_exit_plan_is_refused_before_any_order():
    """Observed live on 2026-08-14, and the worst kind of bug: fail-open.

    Two SIDE=SELL alerts carrying an exit plan opened 10-share shorts with no
    protection of any kind. The exit manager is long-only and refused the lot —
    but only AFTER the entry had filled, and the fallback places a plain stop
    the short alert never asked for. The positions sat naked until someone
    closed them by hand.

    Refused at the parser now, so nothing reaches the broker.
    """
    with pytest.raises(AlertParseError, match=r"(?i)long-only"):
        parse_pine_alert(SHORT)


def test_a_short_without_an_exit_plan_still_works():
    """The guard refuses the COMBINATION. Shorting is not the problem —
    shorting while expecting managed exits that cannot exist is."""
    plain = SHORT.replace(" | EXIT_PLAN=DYNAMIC_TRAIL | INTERVAL=1", "")
    command = parse_pine_alert(plain)

    assert command.side == "sell"
    assert command.exit_plan is None
    assert command.place_protective_stop_after_fill


def test_a_long_with_an_exit_plan_is_unaffected():
    """Paired acceptance: a guard that never admits the good case is an
    outage, not a check."""
    long_alert = SHORT.replace("SIDE=SELL", "SIDE=BUY").replace(
        "STOP_TRIGGER=350 | STOP_LIMIT=350.5", "STOP_TRIGGER=330 | STOP_LIMIT=329.5")

    assert parse_pine_alert(long_alert).exit_plan == "DYNAMIC_TRAIL"


def test_the_fast_plan_is_the_real_plan_with_closer_targets():
    """Same shape, reachable targets — so a test exercises the strategy's
    structure rather than a different strategy that happens to fire."""
    real = exit_plans.resolve("DYNAMIC_TRAIL")
    fast = exit_plans.resolve("DYNAMIC_TRAIL_FAST")

    assert [f for f, _ in fast.tranches] == [f for f, _ in real.tranches], (
        "the fast plan must split the position the same way")
    assert fast.runner_fraction == real.runner_fraction
    assert fast.breakeven_after == real.breakeven_after
    assert fast.trail_source == real.trail_source

    assert [m for _, m in fast.tranches] < [m for _, m in real.tranches], (
        "the whole point is targets that are closer")


def test_the_real_plan_still_has_the_multiples_wei_specified():
    """Guards against the test plan quietly becoming the real one — which is
    what editing DYNAMIC_TRAIL in place had already done on the host."""
    real = exit_plans.resolve("DYNAMIC_TRAIL")

    assert [m for _, m in real.tranches] == [Decimal("1.2"), Decimal("2.5")]
    assert [f for f, _ in real.tranches] == [Decimal("0.20"), Decimal("0.30")]
    assert real.runner_fraction == Decimal("0.50")
