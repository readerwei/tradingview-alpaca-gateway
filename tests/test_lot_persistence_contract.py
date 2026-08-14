"""A lot that has been saved and reloaded must be the lot that was saved.

`dump_lot` listed the plan's fields by hand. `rungs_on_bar_high` was added to
`ExitPlan` later and never added to that list, so it was written by no one and
read back as the dataclass default:

    before save : rungs_on_bar_high=True
    after  load : rungs_on_bar_high=False

That flag is what lets a rung fire on a completed bar's HIGH instead of waiting
for a trade to print at the target. It exists because only 34% of Alpaca's
BTC/USD 1m bars contain any trade at all — it is the mechanism that made TP1
fire on the crypto tests. A lot that had been through a restart quietly lost it.

Silent in every direction: the heartbeat looks identical, the plan name is
still DYNAMIC_TRAIL, and on a liquid symbol nothing is visibly wrong because
trades print constantly. It costs a rung only on the thin feed the flag was
added for, which is the hardest place to notice a missing one.

Every lot is saved after every handler and reloaded by `start()`, so the flag
had never survived a restart since the day it was introduced.

The test that matters here is the round trip on the whole object, not a check
for this one field. A hand-maintained list will be forgotten again; comparing
the reloaded plan to the original cannot be.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from tv_alpaca_gateway import exit_plans
from tv_alpaca_gateway.exit_manager import (ExitPlan, Lot, dump_lot, load_lot,
                                            prefixed)


MIN_SIZE = Decimal("0.000015437")


def _lot(plan_name="DYNAMIC_TRAIL", **kw):
    fields = dict(
        event_id="pine-exec-evt-1", symbol="BTC/USD",
        entry_price=Decimal("63000"), initial_stop=Decimal("62800"),
        held_qty=Decimal("0.001"), timeframe="1m",
        plan=exit_plans.resolve(plan_name), min_order_size=MIN_SIZE)
    fields.update(kw)
    return Lot.opened(**fields)


@pytest.mark.parametrize("plan_name", exit_plans.names())
def test_the_plan_survives_a_save_and_load_unchanged(plan_name):
    """The property, over every plan that ships.

    Compares the whole dataclass rather than named fields: that is what makes
    this catch the NEXT field somebody adds to ExitPlan and forgets to write.
    """
    lot = _lot(plan_name)
    assert load_lot(dump_lot(lot)).plan == lot.plan


@pytest.mark.parametrize("field", [f.name for f in dataclasses.fields(ExitPlan)])
def test_every_plan_field_is_actually_written(field):
    """Names the missing field when it fails, instead of only saying the plans
    differ. `rungs_on_bar_high` was the one that was missing."""
    import json

    plan = json.loads(dump_lot(_lot()))["plan"]
    assert field in plan, f"dump_lot does not persist ExitPlan.{field}"


def test_the_bar_high_flag_specifically_survives():
    """The regression as observed, kept as its own line so a failure reads as
    the bug rather than as an abstract mismatch."""
    lot = _lot("DYNAMIC_TRAIL")
    assert lot.plan.rungs_on_bar_high is True, "the fixture no longer covers the bug"
    assert load_lot(dump_lot(lot)).plan.rungs_on_bar_high is True


def test_a_reloaded_lot_still_fires_a_rung_on_a_bar_high():
    """The consequence, not the field.

    A round-trip assertion proves the flag is stored; this proves the stored
    flag still reaches the decision it exists for — the thin-feed case where no
    trade prints at the target.
    """
    class _Broker:
        def __init__(self):
            self.submitted = []

        def position_qty(self, symbol):
            return Decimal("0.001")

        def submit_order(self, **kw):
            self.submitted.append(kw)
            return {"id": f"ord-{len(self.submitted)}"}

        def cancel_order(self, order_id):
            pass

    lot = load_lot(dump_lot(_lot("DYNAMIC_TRAIL")))
    lot._broker = _Broker()
    target = lot.target_price(1)

    # A bar whose HIGH crosses TP1, with trades in it, but no trade print
    # delivered at the target — the crypto feed's normal case.
    lot.on_bar(high=target + Decimal("50"), low=lot.entry_price,
               close=lot.entry_price, trade_count=7)

    sells = [o for o in lot._broker.submitted if o.get("type") == "market"]
    assert sells, ("a reloaded lot did not fire a rung its plan says a bar high "
                   "should fire")


def test_a_lot_stored_before_the_fix_still_loads():
    """Rows written by the old code have no such key. They must not crash on
    the way in — a lot that cannot be rebuilt is a position with no manager."""
    import json

    state = json.loads(dump_lot(_lot()))
    del state["plan"]["rungs_on_bar_high"]
    lot = load_lot(json.dumps(state))
    assert lot.plan.rungs_on_bar_high is False, (
        "a legacy row should take the dataclass default, not fail or guess")


def test_the_rest_of_the_lot_still_round_trips():
    """Guard against fixing the plan and breaking the lot around it."""
    lot = _lot()
    lot.working_stop = Decimal("62_900")
    lot.remaining_qty = Decimal("0.0008")
    lot.filled_rungs.add(1)
    lot.rung_filled_qty[1] = Decimal("0.0002")
    lot.stop_order_id, lot.stop_generation = "ord-7", 3

    back = load_lot(dump_lot(lot))
    assert back.working_stop == lot.working_stop
    assert back.remaining_qty == lot.remaining_qty
    assert back.filled_rungs == lot.filled_rungs
    assert back.rung_filled_qty == lot.rung_filled_qty
    assert (back.stop_order_id, back.stop_generation) == ("ord-7", 3)
    assert prefixed(back.event_id) == prefixed(lot.event_id)
