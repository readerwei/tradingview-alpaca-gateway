"""The parser's direction checks were written for a long and mirrored by eye.

Two defects, both short-only, both found by generating cases and running them
rather than by reading — which is how the other four in this family were
missed for weeks.

A. The take-profit/stop inversion check has a `side == "buy"` arm and no
   `sell` arm, under a comment claiming it is "the only direction check
   available before a fill price exists". A short with its pair inverted was
   accepted outright.

B. Worse, and more interesting. Two rules contradict:

       exit_plan == "OCO_AFTER_FILL" and stop_limit > stop_trigger  -> refuse
       side == "sell"                and stop_limit < stop_trigger  -> refuse

   The first has no side check and encodes the LONG shape. For a short they
   demand opposite things, so only STOP_LIMIT == STOP_TRIGGER survived both —
   and that is exactly the shape STOP_LIMIT_OFFSET exists to avoid, because a
   trigger and limit at the same price gaps through and never fills.

   Neither rule is wrong when read alone. They are jointly impossible, which no
   amount of reading either one would reveal.
"""

from __future__ import annotations

import pytest

from tv_alpaca_gateway.pine_alert_parser import AlertParseError, parse_pine_alert

BASE = ("EXECUTE_ALPACA_ORDER | SYMBOL=QQQ | QTY=1 | ORDER_TYPE=MARKET | "
        "TIME_IN_FORCE=DAY | EVENT_ID=e1")


def _alert(**fields) -> str:
    return BASE + "".join(f" | {k}={v}" for k, v in fields.items())


def _oco(side, trigger, limit, take_profit):
    return _alert(SIDE=side, EXIT_PLAN="OCO_AFTER_FILL", STOP_TRIGGER=trigger,
                  STOP_LIMIT=limit, TAKE_PROFIT=take_profit)


# ── A: the inversion check, both directions ─────────────────────────────────

def test_a_long_with_take_profit_below_its_stop_is_refused():
    with pytest.raises(AlertParseError, match="inverted"):
        parse_pine_alert(_oco("BUY", "710", "NONE", "700"))


def test_a_short_with_take_profit_above_its_stop_is_refused():
    """The missing arm. A short takes profit BELOW entry and stops ABOVE it, so
    a take-profit above the stop is the pair inverted — the same error the long
    arm has always caught."""
    with pytest.raises(AlertParseError, match="inverted"):
        parse_pine_alert(_oco("SELL", "710", "NONE", "730"))


def test_a_correct_long_pair_is_accepted():
    parse_pine_alert(_oco("BUY", "700", "NONE", "730"))


def test_a_correct_short_pair_is_accepted():
    parse_pine_alert(_oco("SELL", "730", "NONE", "700"))


# ── B: the contradiction ────────────────────────────────────────────────────

def test_a_short_oco_can_express_a_real_stop_limit_offset():
    """The case that was unreachable.

    A short's protective leg is a BUY stop-limit, so the limit sits ABOVE the
    trigger — the buy is willing to pay up to `limit` once `trigger` is touched.
    Every such alert was refused by the OCO rule, and the only accepted shape
    was limit == trigger, which is the one that gaps through without filling.
    """
    parse_pine_alert(_oco("SELL", "710", "715", "700"))


def test_a_short_oco_with_the_limit_below_its_trigger_is_still_refused():
    """The fix must not simply drop the rule: a buy stop-limit whose limit is
    below its trigger can trigger and never fill."""
    with pytest.raises(AlertParseError):
        parse_pine_alert(_oco("SELL", "710", "705", "700"))


def test_a_long_oco_keeps_its_original_rule():
    parse_pine_alert(_oco("BUY", "710", "705", "730"))
    with pytest.raises(AlertParseError):
        parse_pine_alert(_oco("BUY", "710", "715", "730"))


def test_the_two_rules_never_disagree_for_any_side():
    """The property, stated directly: for each side there must exist a stop
    limit that is not equal to the trigger and is accepted. If none does, two
    rules are fighting again."""
    for side, limits in (("BUY", ("700", "705", "709")),
                         ("SELL", ("711", "715", "720"))):
        take_profit = "730" if side == "BUY" else "690"
        accepted = []
        for limit in limits:
            try:
                parse_pine_alert(_oco(side, "710", limit, take_profit))
                accepted.append(limit)
            except AlertParseError:
                pass
        assert accepted, (
            f"no {side} stop-limit offset is expressible; the OCO rule and the "
            f"protective-stop rule contradict for this side")
