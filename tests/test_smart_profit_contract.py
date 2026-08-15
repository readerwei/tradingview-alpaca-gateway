"""SMART_PROFIT: tranches armed by market structure, not by R-multiples.

Wei's specification, and his answers to the four questions it raised:

    Long (everything mirrors for a short). Three slices — 30/40/30. A slice is
    armed by N cumulatively higher lows, where each higher low must be higher
    than ALL previous lows since counting began. Once armed, that slice follows
    the highest such low as a trailing stop and is sold when price breaks it.
    After a slice is taken, the stop moves to breakeven and counting starts
    again from zero for the next slice. If the market weakens — M lower lows —
    every remaining share collapses onto a single 0.1R trailing stop.

    1. A lower low does NOT reset the count. It simply does not increment it.
    2. On weakness, tighten onto a 0.1R trail and let it stop out — not a
       market exit.
    3. A slice may only arm above `entry + 0.5R`. Three higher lows can happen
       entirely underwater, and taking "profit" there is taking a loss.
    4. Counting for the 40% begins only after the 30% has been taken. The other
       way out of waiting is the weakness path.

WHY THIS IS A DIFFERENT MECHANISM, NOT A NEW CONFIG ROW
-------------------------------------------------------
Every other plan fires a rung when price REACHES a level it was given in
advance. This fires a slice when price FALLS BACK THROUGH a level the market
chose, and each slice carries its own level, armed at a different time. One
`working_stop` cannot express three concurrent trails, so the lot grows a small
state machine and the plan grows an `exit_style`.

WHAT THE FEED DOES TO IT
------------------------
This plan consumes BARS, not prints. Over twelve hours of Alpaca's BTC/USD 1m
bars only 34% contained a trade, and a bar with no trades is ignored here for
the same reason it is ignored by the trail: its low is a quote, not a price
anything changed hands at. N=3 on that feed can mean twenty minutes of wall
clock. The same N on TSLA is three minutes.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tv_alpaca_gateway import exit_plans
from tv_alpaca_gateway.exit_manager import ExitPlan, Lot, dump_lot, load_lot


ENTRY = Decimal("100")
STOP = Decimal("90")             # R = 10
R = Decimal("10")
HELD = Decimal("100")
MIN_SIZE = Decimal("0.0001")
GATE = ENTRY + Decimal("0.5") * R        # 105 — nothing arms below this


class _Broker:
    def __init__(self, position=HELD):
        self.position = Decimal(position)
        self.submitted: list[dict] = []
        self.cancelled: list[str] = []
        self.open: dict[str, dict] = {}

    def position_qty(self, symbol):
        return self.position

    def submit_order(self, **kw):
        order_id = f"ord-{len(self.submitted) + 1}"
        self.submitted.append(dict(kw, id=order_id))
        if kw.get("type") in ("stop", "stop_limit"):
            self.open[order_id] = dict(kw, id=order_id)
        return {"id": order_id}

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        self.open.pop(order_id, None)

    def open_orders(self, symbol):
        return list(self.open.values())

    def get_order_by_client_id(self, client_order_id):
        return None

    @property
    def sells(self):
        return [o for o in self.submitted if o.get("type") == "market"]


def _lot(broker, direction=1, entry=ENTRY, stop=STOP, plan_name="SMART_PROFIT"):
    lot = Lot.opened(event_id="pine-exec-evt-1", symbol="TSLA",
                     entry_price=entry, initial_stop=stop, held_qty=HELD,
                     timeframe="1m", plan=exit_plans.resolve(plan_name),
                     min_order_size=MIN_SIZE, direction=direction)
    lot._broker = broker
    lot._resize_stop()
    return lot


def _bar(lot, low, high=None, trades=9):
    """One completed bar. High defaults comfortably above the low so a bar can
    be described by the only thing this plan reads: its low."""
    lot.on_bar(high=high if high is not None else low + 5 * R,
               low=low, close=low + R, trade_count=trades)


def _bars(lot, *lows):
    for low in lows:
        _bar(lot, Decimal(low))


# ── arming ──────────────────────────────────────────────────────────────────

def test_it_takes_n_higher_lows_to_arm_the_first_slice():
    broker = _Broker()
    lot = _lot(broker)

    _bars(lot, "106", "107")           # baseline, then higher low #1
    assert lot.armed_rung is None, "armed before N higher lows"

    _bars(lot, "108")                  # higher low #2
    assert lot.armed_rung is None

    _bars(lot, "109")                  # higher low #3 -> armed
    assert lot.armed_rung == 1
    assert lot.tranche_trail[1] == Decimal("109")


def test_a_lower_low_does_not_reset_the_count():
    """Wei, asked directly: "Just not increment it." A dip in the middle of a
    climb costs a bar, not the whole sequence."""
    broker = _Broker()
    lot = _lot(broker)

    _bars(lot, "106", "107", "103", "108", "101", "109")
    #           base    #1     dip    #2     dip    #3

    assert lot.armed_rung == 1, "a lower low wrongly reset the higher-low count"
    assert lot.tranche_trail[1] == Decimal("109")


def test_a_low_must_beat_every_previous_low_not_just_the_last_one():
    """"every higher low must be higher than all the previous lows" — so a low
    that only beats its immediate predecessor does not count."""
    broker = _Broker()
    lot = _lot(broker)

    _bars(lot, "106", "110", "107", "108", "109")
    #           base    #1     ---    ---    ---     (none beat 110)

    assert lot.armed_rung is None, "a low below the running high-water low counted"


def test_nothing_arms_below_the_profit_gate():
    """Three higher lows can happen entirely underwater. Arming there would
    call a loss a take-profit."""
    broker = _Broker()
    lot = _lot(broker)

    _bars(lot, "92", "93", "94", "95")          # rising, all below entry + 0.5R

    assert lot.armed_rung is None
    assert lot.tranche_trail == {}


def test_the_gate_is_entry_plus_half_r():
    """The boundary itself, so the constant cannot drift silently."""
    broker = _Broker()
    lot = _lot(broker)

    _bars(lot, "101", "102", "103", str(GATE - Decimal("0.01")))
    assert lot.armed_rung is None, "armed just below the gate"

    _bars(lot, str(GATE))
    assert lot.armed_rung == 1, "did not arm at the gate"


# ── trailing and taking a slice ─────────────────────────────────────────────

def test_the_armed_trail_follows_each_new_higher_low():
    broker = _Broker()
    lot = _lot(broker)
    _bars(lot, "106", "107", "108", "109")
    assert lot.tranche_trail[1] == Decimal("109")

    _bars(lot, "112")
    assert lot.tranche_trail[1] == Decimal("112")


def test_an_armed_slice_is_sold_by_the_very_first_lower_low():
    """This test used to assert "the trail never retreats" and passed for the
    wrong reason once bars could break a trail: the trail held at 112 because
    the slice had already been SOLD, not because anything declined to move it.

    The real property, stated plainly because it is the strategy's sharpest
    edge: an armed slice survives only while every bar's low holds at or above
    the highest low so far. The first bar that dips a cent below it ends the
    slice. On a 1m chart that is close to a one-bar trailing stop — which is
    why trailing a confirmed PIVOT low rather than a bar low is the open
    question about this plan.
    """
    broker = _Broker()
    lot = _lot(broker)
    _bars(lot, "106", "107", "108", "109", "112")
    assert lot.tranche_trail[1] == Decimal("112")

    _bars(lot, "111")                       # one dollar below the trail

    assert Decimal(broker.sells[-1]["qty"]) == Decimal("30"), (
        "an armed slice survived a bar that traded below its trail")
    assert lot.tranche_trail[1] == Decimal("112"), "the broken trail moved"


def test_breaking_the_trail_sells_exactly_that_slice():
    broker = _Broker()
    lot = _lot(broker)
    _bars(lot, "106", "107", "108", "109")

    lot.on_price(Decimal("108.9"))          # through the armed low

    assert len(broker.sells) == 1
    assert Decimal(broker.sells[0]["qty"]) == Decimal("30"), "sold the wrong slice"
    assert broker.sells[0]["side"] == "sell"


def test_price_above_the_trail_sells_nothing():
    broker = _Broker()
    lot = _lot(broker)
    _bars(lot, "106", "107", "108", "109")

    lot.on_price(Decimal("109.5"))

    assert not broker.sells


# ── the sequence between slices ─────────────────────────────────────────────

def _take_first_slice(lot, broker):
    _bars(lot, "106", "107", "108", "109")
    lot.on_price(Decimal("108.9"))
    lot.on_fill(rung=1, filled_qty=lot.tranche_qty(1), fill_id="f1")


def test_taking_the_first_slice_moves_the_stop_to_breakeven():
    broker = _Broker()
    lot = _lot(broker)
    _take_first_slice(lot, broker)

    assert lot.working_stop == ENTRY


def test_counting_restarts_from_zero_after_a_slice_is_taken():
    """Wei: the 40% needs N FRESH higher lows. The highs that armed the 30% are
    spent — carrying them over would arm the next slice almost immediately."""
    broker = _Broker()
    lot = _lot(broker)
    _take_first_slice(lot, broker)

    assert lot.armed_rung is None
    assert lot.swing_count == 0

    _bars(lot, "110", "111")            # baseline + one, not yet three
    assert lot.armed_rung is None


def test_the_second_slice_arms_on_its_own_n_higher_lows_and_is_forty_percent():
    broker = _Broker()
    lot = _lot(broker)
    _take_first_slice(lot, broker)

    _bars(lot, "110", "111", "112", "113")
    assert lot.armed_rung == 2

    lot.on_price(Decimal("112.9"))
    assert Decimal(broker.sells[-1]["qty"]) == Decimal("40")


def test_the_third_slice_is_the_remaining_thirty_percent():
    broker = _Broker()
    lot = _lot(broker)
    _take_first_slice(lot, broker)
    _bars(lot, "110", "111", "112", "113")
    lot.on_price(Decimal("112.9"))
    lot.on_fill(rung=2, filled_qty=lot.tranche_qty(2), fill_id="f2")

    _bars(lot, "114", "115", "116", "117")
    assert lot.armed_rung == 3
    lot.on_price(Decimal("113.9"))
    assert Decimal(broker.sells[-1]["qty"]) == Decimal("30")


def test_only_one_slice_is_ever_armed_at_a_time():
    """Wei's answer to question 4. While a slice is trailing, its successor is
    not accumulating — the count belongs to whichever slice is next."""
    broker = _Broker()
    lot = _lot(broker)
    _bars(lot, "106", "107", "108", "109")
    assert lot.armed_rung == 1

    _bars(lot, "110", "111", "112", "113")
    assert lot.armed_rung == 1, "a second slice armed while the first was running"
    assert 2 not in lot.tranche_trail


# ── weakness ────────────────────────────────────────────────────────────────

def test_m_consecutive_lower_lows_collapse_everything_onto_a_tight_trail():
    """"if market starts to weaken ... we will flatten all our remaining
    positions to trail by 0.1R" — tighten and let it stop out, per Wei."""
    broker = _Broker()
    lot = _lot(broker)
    _bars(lot, "106", "107", "108", "109")      # slice 1 armed at 109

    _bars(lot, "108", "107")                    # two consecutive lower lows

    assert lot.weakened is True
    assert lot.armed_rung is None, "a slice trail survived the weakness switch"


def test_the_weak_trail_sits_one_tenth_r_below_the_high_water_mark():
    broker = _Broker()
    lot = _lot(broker)
    _bar(lot, Decimal("106"), high=Decimal("120"))
    _bar(lot, Decimal("105"), high=Decimal("119"))     # lower low 1
    _bar(lot, Decimal("104"), high=Decimal("118"))     # lower low 2 -> weakness

    assert lot.working_stop == Decimal("120") - Decimal("0.1") * R


def test_the_weak_trail_ratchets_up_but_never_down():
    broker = _Broker()
    lot = _lot(broker)
    _bar(lot, Decimal("106"), high=Decimal("120"))
    _bar(lot, Decimal("105"), high=Decimal("119"))
    _bar(lot, Decimal("104"), high=Decimal("118"))
    before = lot.working_stop

    _bar(lot, Decimal("103"), high=Decimal("125"))     # a new extreme
    assert lot.working_stop == Decimal("125") - Decimal("0.1") * R

    _bar(lot, Decimal("102"), high=Decimal("118"))     # a lower one; hold
    assert lot.working_stop == Decimal("125") - Decimal("0.1") * R
    assert lot.working_stop > before


def test_breaking_the_weak_trail_exits_everything_that_is_left():
    broker = _Broker()
    lot = _lot(broker)
    _bar(lot, Decimal("106"), high=Decimal("120"))
    _bars(lot, "105", "104")

    lot.on_price(lot.working_stop - Decimal("0.01"))

    assert lot.stage == "closed"
    assert Decimal(broker.sells[-1]["qty"]) == Decimal("100"), "did not flatten"


def test_a_single_lower_low_is_not_weakness():
    """M is 2 so that one wide bar does not end a trend."""
    broker = _Broker()
    lot = _lot(broker)
    _bars(lot, "106", "107", "108", "109")

    _bars(lot, "108")
    assert lot.weakened is False


def test_the_lower_low_run_must_be_consecutive():
    broker = _Broker()
    lot = _lot(broker)
    _bars(lot, "106", "110")

    _bars(lot, "109", "111", "110")     # down, up, down — never two in a row
    assert lot.weakened is False


def test_weakness_can_arrive_before_anything_has_armed():
    """The other path out of waiting, in Wei's words. A trade that never
    develops must still end up managed rather than sitting on the entry stop."""
    broker = _Broker()
    lot = _lot(broker)
    _bar(lot, Decimal("106"), high=Decimal("112"))
    _bar(lot, Decimal("105"), high=Decimal("111"))
    _bar(lot, Decimal("104"), high=Decimal("110"))

    assert lot.weakened is True
    assert lot.working_stop == Decimal("112") - Decimal("0.1") * R


def test_weakness_never_widens_the_stop():
    """A 0.1R trail computed from a high water mark barely above entry can sit
    BELOW the disaster stop. Adopting it would be paying for weakness with more
    risk, which is the opposite of the intent."""
    broker = _Broker()
    lot = _lot(broker)
    _bar(lot, Decimal("91"), high=Decimal("92"))
    _bars(lot, "90.5", "90.2")

    assert lot.weakened is True
    assert lot.working_stop >= STOP, "weakness moved the stop further from price"


# ── shorts ──────────────────────────────────────────────────────────────────

def test_a_short_arms_on_lower_highs_and_sells_by_buying():
    """Mirrored through the same sign that carries every other decision."""
    broker = _Broker()
    entry, stop = Decimal("100"), Decimal("110")        # R = 10 short
    lot = _lot(broker, direction=-1, entry=entry, stop=stop)

    # For a short the structure is lower HIGHS, and the gate is entry - 0.5R.
    for high in ("94", "93", "92", "91"):
        lot.on_bar(high=Decimal(high), low=Decimal(high) - 5 * R,
                   close=Decimal(high), trade_count=9)

    assert lot.armed_rung == 1
    assert lot.tranche_trail[1] == Decimal("91")

    lot.on_price(Decimal("91.1"))
    assert broker.sells[-1]["side"] == "buy", "a short is closed by buying"
    assert Decimal(broker.sells[-1]["qty"]) == Decimal("30")


def test_a_short_gate_is_below_entry():
    broker = _Broker()
    entry, stop = Decimal("100"), Decimal("110")
    lot = _lot(broker, direction=-1, entry=entry, stop=stop)

    for high in ("99", "98", "97", "96"):           # never below entry - 0.5R
        lot.on_bar(high=Decimal(high), low=Decimal(high) - 5 * R,
                   close=Decimal(high), trade_count=9)

    assert lot.armed_rung is None


# ── the feed, and the plumbing ──────────────────────────────────────────────

def test_a_bar_with_no_trades_is_ignored_entirely():
    """Its low is a quote. Counting it would arm slices off prices nothing ever
    traded at — the same reason the existing trail refuses them."""
    broker = _Broker()
    lot = _lot(broker)

    _bars(lot, "106")
    for low in ("107", "108", "109"):
        _bar(lot, Decimal(low), trades=0)

    assert lot.swing_count == 0
    assert lot.armed_rung is None


def test_the_disaster_stop_still_protects_everything_not_yet_armed():
    broker = _Broker()
    lot = _lot(broker)
    _bars(lot, "106", "107")

    resting = [o for o in broker.submitted if o.get("type") in ("stop", "stop_limit")]
    assert resting, "no protective order rests while the plan waits to arm"
    assert Decimal(str(resting[-1]["stop_price"])) == STOP


def test_the_whole_swing_state_survives_a_save_and_load():
    """A restart mid-trend must not re-count from zero, or forget which slice
    is trailing and at what level."""
    broker = _Broker()
    lot = _lot(broker)
    _bars(lot, "106", "107", "108", "109", "112")

    back = load_lot(dump_lot(lot))

    assert back.armed_rung == lot.armed_rung
    assert back.tranche_trail == lot.tranche_trail
    assert back.swing_count == lot.swing_count
    assert back.swing_reference_low == lot.swing_reference_low
    assert back.weak_count == lot.weak_count
    assert back.weakened == lot.weakened
    assert back.high_water == lot.high_water
    assert back.plan == lot.plan


def test_the_shipped_plan_matches_what_wei_specified():
    plan = exit_plans.resolve("SMART_PROFIT")
    assert [f for f, _ in plan.tranches] == [Decimal("0.30"), Decimal("0.40"),
                                             Decimal("0.30")]
    assert plan.swing_arm_count == 3
    assert plan.swing_weaken_count == 2
    assert plan.swing_min_arm_r == Decimal("0.5")
    assert plan.swing_weak_trail_r == Decimal("0.1")
    assert plan.breakeven_after == 1


@pytest.mark.parametrize("plan_name", ["DYNAMIC_TRAIL", "DYNAMIC_TRAIL_FAST",
                                       "OCO_AFTER_FILL"])
def test_the_existing_plans_are_untouched_by_the_new_mechanism(plan_name):
    """The swing machinery must be inert unless a plan asks for it."""
    assert exit_plans.resolve(plan_name).exit_style == "ladder"


def test_the_heartbeat_reports_swing_state_not_a_meaningless_target(tmp_path, caplog):
    """A swing slice has no price target, so the ladder's `tp1-1.11` reading
    would be a number nothing consults. The heartbeat is the only view of a
    quiet lot — it has to say what the plan is actually waiting for."""
    import logging

    from tv_alpaca_gateway.lot_supervisor import LotSupervisor
    from tv_alpaca_gateway.store import EventStore

    broker = _Broker()
    sup = LotSupervisor(EventStore(tmp_path / "hb.sqlite3"), broker)
    lot = _lot(broker)
    sup.adopt(lot)

    with caplog.at_level(logging.INFO, logger="tv_alpaca_gateway.lot_supervisor"):
        _bars(lot, "106", "107")
        sup.heartbeat()
        arming = caplog.text
        caplog.clear()

        _bars(lot, "108", "109")
        sup.heartbeat()
        armed = caplog.text

    assert "arming 1/3" in arming, arming
    assert "gate=" in arming
    assert "armed" in armed and "trail=109" in armed, armed
    assert "tp1" not in arming, "printed a ladder target for a swing plan"


# ── a bar that traded through a trail (found by TradingBot) ─────────────────

def test_a_bar_that_trades_through_the_trail_sells_the_slice():
    """Waiting for a trade print below the level is the same hole
    `rungs_on_bar_high` closed on the entry side, and worse here: a missed rung
    costs a better fill, a missed trail break leaves the slice riding down
    while the gateway believes it is managed. Two thirds of Alpaca's crypto
    bars deliver no trades, so "we will see a print below it" is not an
    assumption this system may make."""
    broker = _Broker()
    lot = _lot(broker)
    _bars(lot, "106", "107", "108", "109")
    assert lot.tranche_trail[1] == Decimal("109")

    # No on_price call anywhere: the bar alone must do it.
    _bar(lot, Decimal("104"), high=Decimal("110"))

    assert Decimal(broker.sells[-1]["qty"]) == Decimal("30"), (
        "a bar that traded five dollars through the trail sold nothing")


def test_a_bar_through_the_trail_does_not_also_raise_it():
    broker = _Broker()
    lot = _lot(broker)
    _bars(lot, "106", "107", "108", "109")

    _bar(lot, Decimal("104"), high=Decimal("110"))

    assert lot.tranche_trail[1] == Decimal("109"), "the broken trail moved"


def test_a_bar_through_the_weak_trail_flattens_the_remainder():
    """In weakness the whole remainder rides on one trail, so the same rule
    has to hold there or the tightening is decorative."""
    broker = _Broker()
    lot = _lot(broker)
    _bar(lot, Decimal("106"), high=Decimal("130"))
    _bar(lot, Decimal("105"), high=Decimal("129"))
    _bar(lot, Decimal("104"), high=Decimal("128"))       # weakness; trail 129
    assert lot.weakened and lot.working_stop == Decimal("129")

    _bar(lot, Decimal("120"), high=Decimal("126"))       # bar trades through it

    assert lot.stage == "closed"
    assert Decimal(broker.sells[-1]["qty"]) == Decimal("100")


def test_a_short_bar_through_the_trail_sells_the_slice():
    broker = _Broker()
    entry, stop = Decimal("100"), Decimal("110")
    lot = _lot(broker, direction=-1, entry=entry, stop=stop)
    for high in ("94", "93", "92", "91"):
        lot.on_bar(high=Decimal(high), low=Decimal(high) - 5 * R,
                   close=Decimal(high), trade_count=9)
    assert lot.tranche_trail[1] == Decimal("91")

    lot.on_bar(high=Decimal("96"), low=Decimal("90"), close=Decimal("95"),
               trade_count=9)

    assert broker.sells[-1]["side"] == "buy"
    assert Decimal(broker.sells[-1]["qty"]) == Decimal("30")
