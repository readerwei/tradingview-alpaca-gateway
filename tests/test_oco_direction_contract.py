"""An OCO's prices have a direction, and until now only its side did.

`_submit_oco_exit` flips the order side correctly — a short is closed by buying
— but passed `take_profit` and `stop_trigger` through untouched. Alpaca requires
the take-profit on the profitable side of the entry and the stop on the losing
side, which inverts between a long and a short:

    long   take_profit ABOVE   stop BELOW
    short  take_profit BELOW   stop ABOVE

So a short alert carrying long-shaped prices was submitted, rejected by Alpaca,
and the exception routed to `_flatten` — which closed the position. Safe, and
unreadable: it presents as "my short closed instantly and I do not know why",
with an Alpaca rejection string as the only clue.

This was found by review on 2026-08-21 rather than by an incident, because the
short path had never run. It became reachable the moment the entry gate learned
to recognise a short (#65) — before that, no short ever got this far.

The check is relational and needs no market data: whatever the prices are, the
take-profit must sit on the profitable side OF THE STOP for the direction being
traded. Getting that wrong is a strategy error worth naming out loud, not an
opaque broker rejection followed by a surprise flatten.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from tv_alpaca_gateway import execution


@dataclass
class _Broker:
    """Accepts anything. The point is what we DON'T send it."""

    submitted: list[dict] = field(default_factory=list)
    positions: dict = field(default_factory=lambda: {"QQQ": Decimal("-13")})

    def position_qty(self, symbol):
        return self.positions.get(symbol, Decimal("0"))

    def submit_order(self, **kwargs):
        self.submitted.append(dict(kwargs))
        return {"id": f"ord-{len(self.submitted)}", "status": "new"}

    @property
    def ocos(self):
        return [o for o in self.submitted if o.get("order_class") == "oco"]


class _Store:
    def update(self, *a, **k): pass
    def record_broker_order(self, *a, **k): pass


def _command(side, take_profit, stop_trigger):
    from tv_alpaca_gateway.pine_alert_parser import PineOrderCommand
    return PineOrderCommand(
        event_id="evt-1", bar_time=None, symbol="QQQ", side=side,
        qty=Decimal("13"), order_type="market", time_in_force="gtc",
        cancel_unfilled_at_deadline=False, place_protective_stop_after_fill=True,
        stop_trigger=Decimal(stop_trigger), stop_limit=Decimal(stop_trigger),
        trail=None, take_profit=Decimal(take_profit),
        exit_plan="OCO_AFTER_FILL")


def _submit(command, broker):
    return execution._submit_oco_exit(
        command, "QQQ", "entry-1", "filled", "evt-1", broker, _Store(),
        Decimal("13"))


# ── the shapes that are correct ─────────────────────────────────────────────

def test_a_long_with_take_profit_above_and_stop_below_is_submitted():
    broker = _Broker()
    result = _submit(_command("buy", "730", "710"), broker)

    assert result.protection_status == "submitted"
    assert len(broker.ocos) == 1
    assert broker.ocos[0]["side"] == "sell"


def test_a_short_with_take_profit_below_and_stop_above_is_submitted():
    """The mirror, which is the case that had never run."""
    broker = _Broker()
    result = _submit(_command("sell", "707.98", "710.83"), broker)

    assert result.protection_status == "submitted"
    assert len(broker.ocos) == 1
    assert broker.ocos[0]["side"] == "buy", "a short is closed by buying"


# ── the shapes that are wrong ───────────────────────────────────────────────

def test_a_short_carrying_long_shaped_prices_is_refused_before_submission(caplog):
    """The live hazard: take-profit ABOVE and stop BELOW on a short.

    Alpaca would reject it, `_flatten` would close the position, and the
    operator would see an instant unexplained exit. Refuse it here instead,
    naming the direction.
    """
    broker = _Broker()
    with caplog.at_level(logging.WARNING, logger="tv_alpaca_gateway"):
        _submit(_command("sell", "730", "710"), broker)

    assert not broker.ocos, "an inverted OCO was sent to the broker"
    text = caplog.text.lower()
    assert "take" in text and ("short" in text or "direction" in text), (
        f"the refusal does not say what is wrong: {caplog.text}")


def test_a_long_carrying_short_shaped_prices_is_refused_before_submission(caplog):
    broker = _Broker(positions={"QQQ": Decimal("13")})
    with caplog.at_level(logging.WARNING, logger="tv_alpaca_gateway"):
        _submit(_command("buy", "710", "730"), broker)

    assert not broker.ocos, "an inverted OCO was sent to the broker"


def test_equal_prices_are_refused_rather_than_guessed():
    """A take-profit equal to the stop has no profitable side. Whatever the
    author meant, it was not this."""
    broker = _Broker()
    _submit(_command("sell", "710", "710"), broker)

    assert not broker.ocos


def test_a_refused_oco_falls_back_to_the_ordinary_protective_stop():
    """Refusing the OCO must not mean refusing protection.

    The precedent is already in this file: "A ladder that cannot be built is a
    reason to place the ordinary stop, not a reason to leave the position naked
    or to flatten a fill the user wanted." The same reasoning applies to an
    exit plan whose prices point the wrong way — the alert's intent was a
    protected short, and the stop price is still usable even when the
    take-profit is not.
    """
    broker = _Broker()
    result = _submit(_command("sell", "730", "710"), broker)

    assert broker.submitted, (
        "the inverted OCO was refused and nothing was placed in its stead")
    assert result.protection_status == "submitted", (
        "refusing the OCO left the position without protection")
    stops = [o for o in broker.submitted if o.get("order_class") != "oco"]
    assert stops, "no ordinary protective stop was placed"
    assert stops[-1]["side"] == "buy", "a short is protected by a buy stop"
