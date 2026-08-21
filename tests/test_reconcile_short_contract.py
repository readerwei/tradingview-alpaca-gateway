"""Reconciliation read a healthy short as an empty account and closed the lot.

`reconcile_lot` is the restart path — "rebuild from the account, which is the
fact; the database is a cache". It clamps the lot's belief to what the broker
actually holds:

    lot.remaining_qty = min(lot.remaining_qty, position)
    if lot.remaining_qty <= 0:
        lot.stage = "closed"
        lot.stop_order_id, lot.reserved_qty = None, Decimal("0")

`position` is NEGATIVE for a short, so `min(26, -26)` is -26, every short lot
was marked closed, and its stop order id was discarded. The timer runs every
60 seconds, so a short lot survived at most a minute before the gateway stopped
managing it. The position and its stop stayed at Alpaca; nothing in the gateway
knew they existed.

Found by scanning for the CLASS of defect after the 2026-08-21 entry-gate bug,
rather than by an incident. The lesson that day was not "the gate had a sign
error" — it was that short-side code had been written, reviewed, merged and
never executed. This is the same mistake in the restart path, and the clamp is
the one place where being wrong costs a whole lot rather than one order.

Magnitude alone would fix the sign and hide something worse: a lot that is
short while the ACCOUNT is long has not shrunk, it has been reversed, and that
must close the lot rather than let it keep managing a position it no longer
describes. So the clamp measures the position IN THE LOT'S DIRECTION.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tv_alpaca_gateway import exit_plans
from tv_alpaca_gateway.exit_manager import (Lot, dump_lot, load_lot,
                                            reconcile_lot)


class _Broker:
    def __init__(self, position):
        self.position = Decimal(position)
        self.submitted: list[dict] = []

    def position_qty(self, symbol):
        return self.position

    def get_order_by_client_id(self, client_order_id):
        return None

    def open_orders(self, symbol):
        return []

    def submit_order(self, **kwargs):
        self.submitted.append(dict(kwargs))
        return {"id": f"ord-{len(self.submitted)}"}

    def cancel_order(self, order_id):
        pass


def _lot(direction=-1, entry="715", stop="725", held="26"):
    lot = Lot.opened(event_id="pine-exec-evt-1", symbol="QQQ",
                     entry_price=Decimal(entry), initial_stop=Decimal(stop),
                     held_qty=Decimal(held), timeframe="1m",
                     plan=exit_plans.resolve("DYNAMIC_TRAIL"),
                     min_order_size=Decimal("1"), direction=direction)
    return load_lot(dump_lot(lot))          # through the store, as at startup


def test_a_short_lot_survives_reconciliation():
    """The defect, reproduced. A live 26-share short came back CLOSED."""
    lot = reconcile_lot(_lot(direction=-1), _Broker("-26"))

    assert lot.stage != "closed", "a live short lot was reconciled to closed"
    assert lot.remaining_qty == Decimal("26"), (
        f"remaining_qty is {lot.remaining_qty}; a short position is negative "
        f"and must be measured as a magnitude in the lot's direction")


def test_a_short_lot_keeps_being_protected_after_reconciliation():
    """Closing the lot also discarded `stop_order_id`, so the gateway forgot
    the resting stop existed. That is the part that costs money."""
    broker = _Broker("-26")
    lot = reconcile_lot(_lot(direction=-1), broker)

    assert lot.stop_order_id is not None or broker.submitted, (
        "the short lot ended reconciliation with no protective order and no "
        "attempt to place one")


def test_a_long_lot_is_unaffected():
    """The direction that always worked, pinned so the fix cannot break it."""
    lot = reconcile_lot(_lot(direction=1, entry="715", stop="705"),
                        _Broker("26"))

    assert lot.stage != "closed"
    assert lot.remaining_qty == Decimal("26")


def test_a_partly_closed_short_is_clamped_to_what_is_left():
    """The clamp still has its job: the lot must never believe it holds more
    than the account does."""
    lot = reconcile_lot(_lot(direction=-1), _Broker("-10"))

    assert lot.remaining_qty == Decimal("10")


def test_a_flat_account_closes_a_short_lot():
    """Someone covered the short by hand. The lot must not keep managing it."""
    lot = reconcile_lot(_lot(direction=-1), _Broker("0"))

    assert lot.stage == "closed"


def test_a_reversed_position_closes_the_lot_rather_than_being_taken_as_size():
    """The reason the clamp uses direction rather than magnitude.

    A lot that is short while the account is LONG has not shrunk, it has been
    reversed. `abs()` would read +26 as "still 26 short" and carry on managing
    a position that no longer exists in that direction — arming rungs and
    resizing a stop against the wrong side of the market.
    """
    lot = reconcile_lot(_lot(direction=-1), _Broker("26"))

    assert lot.stage == "closed", (
        "a short lot survived the account flipping long; magnitude was used "
        "where direction was needed")


def test_breakeven_on_reconcile_is_measured_in_the_lot_s_direction():
    """`advance_to_runner` and `on_fill` both compare through `sign`; this path
    did not. For a short the stop sits ABOVE entry, so the unsigned comparison
    never fired and breakeven was silently skipped on any lot that restarted.
    """
    lot = _lot(direction=-1)
    lot.rung_filled_qty[1] = lot.tranche_qty(1)
    lot.filled_rungs.add(1)
    lot.working_stop = Decimal("730")            # above entry, as a short's is

    class _Filled(_Broker):
        def get_order_by_client_id(self, client_order_id):
            if client_order_id.endswith("-tp1"):
                return {"filled_qty": str(lot.tranche_qty(1))}
            return None

    back = reconcile_lot(lot, _Filled("-20"))

    assert back.working_stop == back.entry_price, (
        f"breakeven was not applied on reconcile: stop {back.working_stop} "
        f"vs entry {back.entry_price}")
