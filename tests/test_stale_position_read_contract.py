"""A confirmed fill and a zero position delta cannot both be true.

`_protect_or_flatten` read `position_qty()` ONCE and, if the delta was <= 0,
returned a success-shaped result before reaching ANY protection — native OCO,
the managed ladder, the fallback stop, and `_flatten` itself all sit below that
gate.

That is correct when the entry genuinely added nothing. It is catastrophic when
the read is merely STALE: Alpaca's positions endpoint is eventually consistent
with respect to order status, so a fill confirmed by `/v2/orders` can be
followed by a `/v2/positions` that has not caught up. The entry is real, the
gate says "nothing to protect", and the position is left naked with a WARNING
and `protection_status=None`.

The asymmetry was backwards. PROTECTION_ATTEMPTS gives the protective ORDER two
attempts, and its docstring names "a position not yet settled broker-side" as
the reason — but that case returned above the retry loop, so the retry could
never help the thing it was written for.

Found by an independent review agent on 2026-08-21 that was told to execute code
rather than read it. Three human-equivalent reviewers had read this function
that afternoon while fixing a different bug in the same gate.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from tv_alpaca_gateway import execution


class _LaggingBroker:
    """Confirms the fill, then reports the position one call late.

    Exactly Alpaca's documented behaviour: order status and position are not
    updated atomically.
    """

    def __init__(self, lag_calls: int = 1, final=Decimal("10")):
        self.calls = 0
        self.lag_calls = lag_calls
        self.final = final
        self.submitted: list[dict] = []

    def position_qty(self, symbol):
        self.calls += 1
        return Decimal("0") if self.calls <= self.lag_calls else self.final

    def submit_order(self, **kwargs):
        self.submitted.append(dict(kwargs))
        return {"id": f"ord-{len(self.submitted)}", "status": "new"}

    def cancel_order(self, order_id):
        pass

    @property
    def protective(self):
        return [o for o in self.submitted
                if o.get("type") in ("stop", "stop_limit") or o.get("order_class")]


class _Store:
    def __init__(self):
        self.updates: list[tuple] = []

    def update(self, *a, **k):
        self.updates.append(a)

    def record_broker_order(self, *a, **k):
        pass


def _command(side="buy"):
    from tv_alpaca_gateway.pine_alert_parser import PineOrderCommand
    return PineOrderCommand(
        event_id="evt-1", bar_time=None, symbol="QQQ", side=side,
        qty=Decimal("10"), order_type="market", time_in_force="gtc",
        cancel_unfilled_at_deadline=False, place_protective_stop_after_fill=True,
        stop_trigger=Decimal("700"), stop_limit=Decimal("699"), trail=None)


def _protect(broker, store, side="buy"):
    return execution._protect_or_flatten(
        _command(side), "QQQ", False, "entry-1", "filled", "evt-1", broker,
        store, position_before=Decimal("0"))


def test_a_lagging_position_read_does_not_leave_the_position_naked():
    """The finding, reproduced. One stale read used to end the whole path."""
    broker = _LaggingBroker(lag_calls=1)
    result = _protect(broker, _Store())

    assert broker.protective, (
        "a stale position read skipped protection entirely; the entry filled "
        "and nothing rests at the broker")
    assert result.protection_status == "submitted"


def test_the_protective_quantity_is_what_the_settled_read_shows():
    """Retrying must not protect a guess. The size has to come from the read
    that actually saw the position."""
    broker = _LaggingBroker(lag_calls=1, final=Decimal("10"))
    _protect(broker, _Store())

    assert Decimal(str(broker.protective[-1]["qty"])) == Decimal("10")


def test_a_short_entry_survives_a_lagging_read_too():
    broker = _LaggingBroker(lag_calls=1, final=Decimal("-10"))
    _protect(broker, _Store(), side="sell")

    assert broker.protective, "a stale read on a SHORT skipped protection"
    assert broker.protective[-1]["side"] == "buy"
    assert Decimal(str(broker.protective[-1]["qty"])) == Decimal("10")


def test_a_genuinely_empty_delta_is_still_reported_and_still_stops():
    """The gate must keep working when the entry really did add nothing —
    a retry must not turn a correct refusal into an order for zero."""
    broker = _LaggingBroker(lag_calls=99, final=Decimal("0"))
    result = _protect(broker, _Store())

    assert not broker.protective, "protected a position that does not exist"
    assert result.protection_status != "submitted"


def test_a_persistent_zero_after_a_confirmed_fill_is_reported_as_an_anomaly(caplog):
    """A filled entry and a zero delta cannot both be true. After retries it is
    no longer a lag, it is a contradiction — and the old code reported it at
    WARNING with a success-shaped result, which reads as 'nothing to do'."""
    broker = _LaggingBroker(lag_calls=99, final=Decimal("0"))
    with caplog.at_level(logging.ERROR, logger="tv_alpaca_gateway"):
        _protect(broker, _Store())

    assert any(r.levelno >= logging.ERROR for r in caplog.records), (
        "a confirmed fill with no position after retries was reported at "
        "WARNING; it is an unexplained state, not a routine one")


def test_the_read_is_retried_more_than_once():
    """The property, stated so it cannot regress to a single read."""
    broker = _LaggingBroker(lag_calls=99, final=Decimal("0"))
    _protect(broker, _Store())

    assert broker.calls > 1, (
        f"position_qty was called {broker.calls} time(s); a single read decides "
        f"whether to protect at all, while the protective order itself gets "
        f"{execution.PROTECTION_ATTEMPTS} attempts")
