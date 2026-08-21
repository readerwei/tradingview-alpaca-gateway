"""The flatten path is the last line of defence, and its fallback was unsafe.

`_flatten` closes a position that could not be protected — it is what converts
"filled and unprotected" into a realised loss of known size. It took an
optional `held_qty` and, when not given one, read the whole position:

    if held_qty is None:
        held_qty = _decimal(broker.position_qty(symbol))
    if held_qty <= 0:
        return ...("failed_no_position")

That default was wrong in two independent ways, and the second contradicts the
comment written directly above it.

1. SIGN. A short position reports a NEGATIVE quantity, so `held_qty <= 0` was
   true for every short and the function returned "no position" — declining to
   flatten a real short. The same mistake that left QQQ -26 unprotected on
   2026-08-21, sitting in the safety net meant to catch it.

2. SCOPE. `position_qty` is the WHOLE position, not what this entry added. The
   comment above it reads "Close what this entry added, not the whole position
   — flattening someone else's holdings to unwind our own failure would be
   worse than the failure." The fallback did precisely that. On this account it
   matters concretely: a second system trades the same symbols, so unwinding a
   13-share failure could have closed its 100 shares too.

Both callers pass `held_qty` today, so neither had fired. The fix is to remove
the default rather than repair it: a caller that cannot say how much this entry
added has no business guessing, and a TypeError is a better outcome than a
silent wrong quantity in the one function whose job is to make things safe.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

from tv_alpaca_gateway import execution


class _Broker:
    """Refuses to protect, so every test here reaches the flatten path."""

    def __init__(self, position=Decimal("-26")):
        self.position = position
        self.submitted: list[dict] = []

    def position_qty(self, symbol):
        return self.position

    def submit_order(self, **kwargs):
        if kwargs.get("type") in ("stop", "stop_limit") or kwargs.get("order_class"):
            raise RuntimeError("broker refused the protective order")
        self.submitted.append(kwargs)
        return {"id": f"ord-{len(self.submitted)}", "status": "new"}

    @property
    def flattens(self):
        return [o for o in self.submitted
                if str(o.get("client_order_id", "")).endswith("-flatten")]


class _Store:
    def update(self, *a, **k): pass
    def record_broker_order(self, *a, **k): pass


def _command(side="sell"):
    from tv_alpaca_gateway.pine_alert_parser import PineOrderCommand
    return PineOrderCommand(
        event_id="evt-1", bar_time=None, symbol="QQQ", side=side,
        qty=Decimal("13"), order_type="market", time_in_force="gtc",
        cancel_unfilled_at_deadline=False, place_protective_stop_after_fill=True,
        stop_trigger=Decimal("720"), stop_limit=Decimal("720"), trail=None)


def test_a_short_that_cannot_be_protected_is_actually_flattened():
    """The QQQ -26 case. A short reports a negative quantity, and the guard
    that reads it must not conclude there is nothing to close."""
    broker = _Broker(position=Decimal("-26"))
    execution._flatten(_command("sell"), "QQQ", "entry-1", "filled", "evt-1",
                       broker, _Store(), RuntimeError("protection failed"),
                       Decimal("13"))

    assert broker.flattens, "a short position was not flattened"
    order = broker.flattens[-1]
    assert order["side"] == "buy", "a short is closed by buying"
    assert Decimal(str(order["qty"])) == Decimal("13")


def test_it_closes_what_this_entry_added_not_the_whole_position():
    """The account is shared with another system. Unwinding our own failure
    must not close holdings we did not open."""
    broker = _Broker(position=Decimal("-113"))     # 100 theirs, 13 ours
    execution._flatten(_command("sell"), "QQQ", "entry-1", "filled", "evt-1",
                       broker, _Store(), RuntimeError("protection failed"),
                       Decimal("13"))

    assert Decimal(str(broker.flattens[-1]["qty"])) == Decimal("13"), (
        "flattened more than this entry added")


def test_a_long_that_cannot_be_protected_is_still_flattened():
    """The direction that always worked, kept so the fix cannot break it."""
    broker = _Broker(position=Decimal("92"))
    execution._flatten(_command("buy"), "AAPL", "entry-1", "filled", "evt-1",
                       broker, _Store(), RuntimeError("protection failed"),
                       Decimal("92"))

    assert broker.flattens[-1]["side"] == "sell"
    assert Decimal(str(broker.flattens[-1]["qty"])) == Decimal("92")


def test_flatten_cannot_be_called_without_saying_how_much():
    """The fix, stated as a contract rather than as an implementation detail.

    A caller that cannot say how much this entry added has no business
    guessing, and the old default guessed badly in both directions. Removing
    it makes the unsafe call impossible instead of merely incorrect.
    """
    signature = inspect.signature(execution._flatten)
    held = signature.parameters["held_qty"]
    assert held.default is inspect.Parameter.empty, (
        "_flatten still has a default held_qty; a caller can omit it and get "
        "the whole position, of the wrong sign")


def test_nothing_is_submitted_when_the_entry_added_nothing():
    """The guard still has a job: a delta of zero means this entry filled
    nothing, and there is genuinely nothing to close."""
    broker = _Broker(position=Decimal("-26"))
    result = execution._flatten(_command("sell"), "QQQ", "entry-1", "filled",
                                "evt-1", broker, _Store(),
                                RuntimeError("protection failed"), Decimal("0"))

    assert not broker.flattens
    assert result.protection_status == "failed_no_position"
