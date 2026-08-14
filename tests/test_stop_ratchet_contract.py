"""The broker's stop follows the software stop, in coarse steps.

Wei, reading a live run: "how do you manage the trail in the last 50%? I didn't
see you making any update to the order for trailing?"

He had not missed anything. The trail was a number held in the gateway and
fired as a market sell when breached, and the resting broker stop never moved
off the disaster level for the life of the lot — visible in his own log, where
the re-place after TP1 changed the size and left the price alone:

    16:39:31  protection 0         -> 0.0009975 at 62800
    16:42:24  protection 0.0009975 -> 0.000798  at 62800
                                                   ^^^^^ never moves

That was his own call ("disaster stop stay, and you should move your own
stop"), and per-bar order churn is a bad trade: a cancel-then-place every
minute is sixty naked windows an hour, for a stop that usually moves pennies.

But the gap it leaves grows with the size of the win. At 16:44 his two stops
were 62,800 at Alpaca and 62,851.96 in the gateway — $52, and that was only
breakeven. A runner up 3R has the entire trailed gain living in a process that
can die. The asymmetry is the problem: the crash costs most exactly when the
trade is going best.

So the broker's stop is ratcheted only when the software stop has pulled ahead
by a material amount — 0.5R by default, in the plan config. Two or three order
updates over a runner's life instead of one a minute, and the unprotected gap
is bounded at 0.5R instead of unbounded.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from tv_alpaca_gateway import exit_plans
from tv_alpaca_gateway.exit_manager import ExitPlan, Lot, dump_lot, load_lot


ENTRY = Decimal("100")
STOP = Decimal("90")            # R = 10
R = Decimal("10")
HELD = Decimal("30")
MIN_SIZE = Decimal("0.0001")


class _Broker:
    """Refuses what Alpaca refuses: a resting stop reserves the position, so
    nothing can be sold until it is cancelled or resized."""

    def __init__(self, position=HELD):
        self.position = Decimal(position)
        self.submitted: list[dict] = []
        self.cancelled: list[str] = []
        self.open: dict[str, dict] = {}
        self.fail_next_submit = False

    def position_qty(self, symbol):
        return self.position

    def submit_order(self, **kw):
        if self.fail_next_submit:
            self.fail_next_submit = False
            raise RuntimeError("broker refused the order")
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

    def recent_bars(self, symbol, timeframe):
        return []

    @property
    def stops(self):
        return [o for o in self.submitted if o.get("type") in ("stop", "stop_limit")]

    def stop_prices(self):
        return [Decimal(str(o.get("stop_price"))) for o in self.stops]


def _runner_lot(broker, ratchet=Decimal("0.5"), direction=1, entry=ENTRY,
                stop=STOP):
    plan = ExitPlan(
        name="TEST", tranches=((Decimal("0.5"), Decimal("1")),),
        runner_fraction=Decimal("0.5"),
        trail_source="previous_completed_bar_low", breakeven_after=1,
        rungs_on_bar_high=True, stop_ratchet_r=ratchet)
    lot = Lot.opened(event_id="pine-exec-evt-1", symbol="TSLA",
                     entry_price=entry, initial_stop=stop, held_qty=HELD,
                     timeframe="1m", plan=plan, min_order_size=MIN_SIZE,
                     direction=direction)
    lot._broker = broker
    lot.stage = "runner"
    lot.remaining_qty = HELD
    lot._resize_stop()                   # the disaster stop rests
    return lot


# ── it moves, and only when it is worth moving ──────────────────────────────

def test_a_small_trail_move_does_not_touch_the_broker(caplog):
    """The whole point of a threshold. A stop that moved pennies would produce
    a cancel-then-place, and a naked window, for nothing."""
    broker = _Broker()
    lot = _runner_lot(broker)
    before = len(broker.stops)

    # +0.2R above the resting stop: real, and not worth an order.
    lot.on_bar(high=ENTRY + 3 * R, low=STOP + Decimal("2"), close=ENTRY + 3 * R,
               trade_count=9)

    assert lot.working_stop == STOP + Decimal("2"), "the software stop did not trail"
    assert len(broker.stops) == before, "a sub-threshold move re-placed the stop"


def test_a_trail_move_past_the_threshold_moves_the_resting_stop():
    """The behaviour Wei asked for: an order update he can see."""
    broker = _Broker()
    lot = _runner_lot(broker)

    lot.on_bar(high=ENTRY + 3 * R, low=STOP + Decimal("6"), close=ENTRY + 3 * R,
               trade_count=9)

    assert broker.stop_prices()[-1] == STOP + Decimal("6"), (
        "the resting stop did not follow the software stop past the threshold")
    assert lot.resting_stop == STOP + Decimal("6")


def test_the_old_stop_is_cancelled_before_the_new_one_is_placed():
    """Alpaca reserves the whole position behind a resting stop, so there is no
    available quantity for a replacement while the old one rests. Cancel-then-
    place is not a preference here."""
    broker = _Broker()
    lot = _runner_lot(broker)
    first = lot.stop_order_id

    lot.on_bar(high=ENTRY + 3 * R, low=STOP + Decimal("6"), close=ENTRY + 3 * R,
               trade_count=9)

    assert first in broker.cancelled, "the previous stop was left resting"
    assert lot.stop_order_id != first


def test_the_resting_stop_never_moves_backwards():
    """A trail that can retreat is not a trail. The software stop is already
    monotonic; this asserts the broker's copy inherits that."""
    broker = _Broker()
    lot = _runner_lot(broker)
    lot.on_bar(high=ENTRY + 3 * R, low=STOP + Decimal("6"), close=ENTRY + 3 * R,
               trade_count=9)
    high_water = lot.resting_stop
    placed = len(broker.stops)

    # A lower bar low. The software stop ignores it, so the broker's must too.
    lot.on_bar(high=ENTRY + 2 * R, low=STOP + Decimal("1"), close=ENTRY + 2 * R,
               trade_count=9)

    assert lot.resting_stop == high_water
    assert len(broker.stops) == placed, "a retreating bar re-placed the stop"


def test_the_ratchet_is_off_when_the_plan_does_not_ask_for_it():
    """Wei's current behaviour stays available: a plan with no ratchet keeps the
    disaster stop where it was placed, for the whole life of the lot."""
    broker = _Broker()
    lot = _runner_lot(broker, ratchet=Decimal("0"))
    placed = len(broker.stops)

    lot.on_bar(high=ENTRY + 5 * R, low=ENTRY + 3 * R, close=ENTRY + 5 * R,
               trade_count=9)

    assert lot.resting_stop == STOP
    assert len(broker.stops) == placed


def test_it_does_not_ratchet_while_still_climbing_the_ladder():
    """Before the runner stage the position is still being sold down in
    tranches, and every rung already resizes the stop. Moving its price mid-
    ladder would add order churn to the busiest part of the lifecycle.

    Asserted on the stop's PRICE, not on how many were placed: a rung firing
    re-places the stop at a new SIZE, which is the ladder working correctly.
    Counting orders here would fail for the right behaviour.
    """
    broker = _Broker()
    lot = _runner_lot(broker)
    lot.stage = "ladder"

    lot.on_bar(high=ENTRY + 5 * R, low=ENTRY + 3 * R, close=ENTRY + 5 * R,
               trade_count=9)

    assert lot.stage == "ladder", "the fixture no longer covers the mid-ladder case"
    assert lot.resting_stop == STOP
    assert set(broker.stop_prices()) == {STOP}, "the stop's price moved mid-ladder"


# ── it must never make things worse ─────────────────────────────────────────

def test_it_never_places_a_stop_the_market_has_already_passed():
    """The trap breakeven fell into on 2026-08-11: a stop set beyond the market
    is a market exit wearing a stop's name. `_apply_breakeven` learned to defer;
    this must not re-learn it the hard way."""
    broker = _Broker()
    lot = _runner_lot(broker)
    lot.last_price = STOP + Decimal("3")        # price has fallen below the new stop

    lot.on_bar(high=ENTRY + 3 * R, low=STOP + Decimal("6"), close=ENTRY + 3 * R,
               trade_count=9)

    for price in broker.stop_prices():
        assert price <= lot.last_price, (
            f"placed a sell stop at {price} with the market at {lot.last_price}")


def test_a_failed_replacement_leaves_the_lot_telling_the_truth(caplog):
    """The genuine cost of this feature: cancel-then-place has a window, and a
    refused replacement leaves the position with no resting stop at all.

    It must not then believe it is protected — the 60-second reconcile re-places
    from `_resize_stop`, and it can only do that if the lot admits the stop is
    gone. Silently keeping the old order id would leave it unprotected until
    somebody read a chart.
    """
    broker = _Broker()
    lot = _runner_lot(broker)
    broker.fail_next_submit = True

    with caplog.at_level(logging.ERROR, logger="tv_alpaca_gateway"):
        lot.on_bar(high=ENTRY + 3 * R, low=STOP + Decimal("6"),
                   close=ENTRY + 3 * R, trade_count=9)

    assert lot.stop_order_id is None, "the lot still points at a stop it does not have"
    assert lot.reserved_qty == 0
    assert "unprotected" in caplog.text.lower(), (
        "a failed ratchet was not reported at ERROR")


def test_a_failed_ratchet_is_repaired_by_the_next_resize():
    """Which is what the reconcile timer calls. The naked window is bounded by
    the reconcile interval rather than lasting until a human notices."""
    broker = _Broker()
    lot = _runner_lot(broker)
    broker.fail_next_submit = True
    lot.on_bar(high=ENTRY + 3 * R, low=STOP + Decimal("6"), close=ENTRY + 3 * R,
               trade_count=9)
    assert lot.stop_order_id is None

    lot._resize_stop()                    # what reconcile_lot ends with

    assert lot.stop_order_id is not None, "the lot was not re-protected"
    assert lot.reserved_qty == lot.remaining_qty


def test_the_software_stop_still_fires_the_exit():
    """The ratchet adds a broker-side floor; it does not replace the gateway's
    own stop, which is still the tighter of the two between ratchets."""
    broker = _Broker()
    lot = _runner_lot(broker)
    lot.on_bar(high=ENTRY + 3 * R, low=STOP + Decimal("6"), close=ENTRY + 3 * R,
               trade_count=9)
    lot.working_stop = STOP + Decimal("8")        # tighter than the resting copy

    lot.on_price(STOP + Decimal("7"))

    assert lot.stage == "closed"
    assert any(o.get("type") == "market" for o in broker.submitted)


# ── shorts, and the round trip ──────────────────────────────────────────────

def test_a_short_ratchets_downward():
    """Mirrored, via the same sign that carries every other decision."""
    broker = _Broker()
    entry, stop = Decimal("100"), Decimal("110")      # R = 10 for a short
    lot = _runner_lot(broker, direction=-1, entry=entry, stop=stop)

    lot.on_bar(high=stop - Decimal("6"), low=entry - 3 * R, close=entry - 3 * R,
               trade_count=9)

    assert lot.resting_stop == stop - Decimal("6")
    assert broker.stop_prices()[-1] == stop - Decimal("6")
    assert broker.stops[-1]["side"] == "buy", "a short is protected by a buy stop"


def test_the_resting_stop_survives_a_save_and_load():
    """Otherwise a restart believes the broker's stop is still at the disaster
    level, and the next ratchet is measured from the wrong place — it would
    re-place an order that is already where it needs to be, or skip one it
    needs."""
    broker = _Broker()
    lot = _runner_lot(broker)
    lot.on_bar(high=ENTRY + 3 * R, low=STOP + Decimal("6"), close=ENTRY + 3 * R,
               trade_count=9)

    back = load_lot(dump_lot(lot))
    assert back.resting_stop == lot.resting_stop
    assert back.plan.stop_ratchet_r == lot.plan.stop_ratchet_r


def test_a_lot_stored_before_this_change_still_loads():
    """Legacy rows have no resting stop recorded. The disaster stop is where it
    would have been, so that is the honest default."""
    import json

    broker = _Broker()
    lot = _runner_lot(broker)
    state = json.loads(dump_lot(lot))
    del state["resting_stop"]
    del state["plan"]["stop_ratchet_r"]

    back = load_lot(json.dumps(state))
    assert back.resting_stop == back.initial_stop
    assert back.plan.stop_ratchet_r == 0


@pytest.mark.parametrize("plan_name", ["DYNAMIC_TRAIL", "DYNAMIC_TRAIL_FAST"])
def test_the_shipped_trail_plans_ratchet(plan_name):
    """A plan with a runner and no ratchet is the unbounded-gap case this
    change exists to close."""
    plan = exit_plans.resolve(plan_name)
    assert plan.stop_ratchet_r > 0, f"{plan_name} leaves its runner's gain unprotected"


def test_a_plan_with_no_runner_does_not_ratchet():
    """OCO_AFTER_FILL sells the whole position at one target. There is no
    runner to trail, so a ratchet threshold would be dead configuration."""
    assert exit_plans.resolve("OCO_AFTER_FILL").stop_ratchet_r == 0
