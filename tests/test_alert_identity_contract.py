"""Contract for alert identity and freshness on the Pine path.

WHY THIS EXISTS
---------------
`_command_id` hashes only the order fields — symbol, side, qty, order type,
time in force, and the stop values. Wei's alert carries nothing else, so two
firings of the same setup produce the same id:

    Monday 09:31   QQQ BUY 1 MARKET DAY  ->  pine-exec-fa4336eea2e7435c...
    Tuesday 10:14  QQQ BUY 1 MARKET DAY  ->  pine-exec-fa4336eea2e7435c...

The second is refused as a duplicate and places nothing. That inverts what
idempotency is for: it should stop one firing being delivered twice, not stop
a strategy from producing the same signal twice. A systematic strategy emitting
identical signals is the normal case.

It is a regression, which is the tell. The JSON path required six fields:

    {"event_id", "symbol", "action", "timeframe", "bar_time", "close"}

`event_id` made each firing unique and `bar_time` made it checkable. The pipe
format has neither, and both protections left with them — including freshness,
so a stale alert replayed after a relay restart is indistinguishable from a
live one.

FORMAT-AGNOSTIC ON PURPOSE
--------------------------
Nobody has yet seen what TradingView actually renders for `{{time}}` or
`{{timenow}}`. Every assertion here is about a PROPERTY that holds whatever the
format turns out to be. Only `_BAR_TIME` below encodes a guess, and it is
isolated so that one line changes when the real payload arrives.

Every expected rejection matches on its REASON. Without that, all of them
passed against today's parser — which refuses `EVENT_ID` and `BAR_TIME` as
unrecognised fields, satisfying a bare `raises(Exception)` while proving
nothing about freshness. Fourth time that trap has appeared in tests written
to catch it; the positive case (`test_a_current_alert_is_accepted`) is what
exposed it, which is the argument for always pairing a refusal with an
acceptance.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

parser = pytest.importorskip(
    "tv_alpaca_gateway.pine_alert_parser",
    reason="identity fields not yet in the parser")

# The single guess in this file. Replace with the real rendering once a
# TradingView payload has been captured; nothing else should need to change.
_BAR_TIME = "{:%Y-%m-%dT%H:%M:%SZ}"


def _alert(event_id="QQQ-1-1754665800", bar_time=None, **over):
    when = bar_time if bar_time is not None else _BAR_TIME.format(
        datetime.now(timezone.utc))
    fields = {
        "SYMBOL": "QQQ", "SIDE": "BUY", "QTY": "1",
        "ORDER_TYPE": "MARKET", "TIME_IN_FORCE": "DAY",
        "EVENT_ID": event_id, "BAR_TIME": when,
    }
    fields.update(over)
    body = " | ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    return f"EXECUTE_ALPACA_ORDER | {body}"


def _id(alert):
    execution = pytest.importorskip("tv_alpaca_gateway.execution")
    return execution._command_id(parser.parse_pine_alert(alert))


# ═══════════════════════════════════════════════════════════════ identity

def test_two_firings_of_the_same_setup_are_different_orders():
    """The bug this file exists for.

    Same symbol, side, size and prices — a different firing. It must be able to
    trade, or a strategy that repeats a signal can only ever trade once.
    """
    monday = _alert(event_id="QQQ-1-1754665800")
    tuesday = _alert(event_id="QQQ-1-1754752200")

    assert _id(monday) != _id(tuesday), (
        "two separate firings collapsed to one id; the second would be "
        "refused as a duplicate and place nothing")


def test_the_same_firing_delivered_twice_is_one_order():
    """And the half that must survive: Discord redelivery, a relay restart, a
    doubled webhook. Same EVENT_ID, same id, one order."""
    alert = _alert(event_id="QQQ-1-1754665800")
    assert _id(alert) == _id(alert)


def test_identity_comes_from_the_event_id_not_the_order_fields():
    """Two alerts differing only in size are still two firings — but if the id
    is derived from the order contents, changing any field silently changes
    identity, and a strategy that resizes would bypass duplicate protection."""
    same_firing_a = _alert(event_id="QQQ-1-1754665800", QTY="1")
    same_firing_b = _alert(event_id="QQQ-1-1754665800", QTY="2")

    assert _id(same_firing_a) == _id(same_firing_b), (
        "identity still depends on order contents rather than EVENT_ID")


def test_an_alert_without_an_event_id_is_refused():
    """Falling back to hashing the order fields is what produced the bug. An
    alert that cannot be identified must be refused, not guessed at."""
    with pytest.raises(parser.AlertParseError, match=r"(?i)EVENT_ID"):
        parser.parse_pine_alert(_alert(EVENT_ID=None))


# ══════════════════════════════════════════════════════════════ freshness

def test_a_stale_alert_is_refused():
    """No BAR_TIME means a message from this morning, re-forwarded after a
    relay restart, is indistinguishable from a live signal. The JSON path has
    had a 180-second rule from the start; the Pine path needs the same one."""
    old = _BAR_TIME.format(datetime.now(timezone.utc) - timedelta(hours=2))
    with pytest.raises(Exception, match=r"(?i)stale|old|age|fresh"):
        parser.parse_pine_alert(_alert(bar_time=old))


def test_an_alert_from_the_future_is_refused():
    """A clock skewed forward would otherwise let an alert stay 'fresh'
    indefinitely."""
    ahead = _BAR_TIME.format(datetime.now(timezone.utc) + timedelta(hours=1))
    with pytest.raises(Exception, match=r"(?i)future|ahead|skew|fresh"):
        parser.parse_pine_alert(_alert(bar_time=ahead))


def test_a_current_alert_is_accepted():
    """The rule has to admit the normal case, or it is just an outage."""
    parser.parse_pine_alert(_alert())


def test_an_unparseable_bar_time_is_refused_not_ignored():
    """If the rendered format is not what we expect, that must fail loudly.

    Treating an unreadable timestamp as 'no timestamp' and proceeding is how a
    freshness check quietly stops existing — which is exactly how the Pine path
    lost the one it inherited.
    """
    with pytest.raises(Exception, match=r"(?i)BAR_TIME|timestamp|time"):
        parser.parse_pine_alert(_alert(bar_time="not-a-timestamp"))
