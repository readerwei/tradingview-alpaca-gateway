from __future__ import annotations

import re

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .assets import is_crypto


class AlertParseError(ValueError):
    """The alert is not an executable Pine/TradingView order command."""


_PREFIX = "EXECUTE_ALPACA_ORDER"
# Discord's authenticated message snowflake is the per-firing identity for the
# relay path. EVENT_ID and BAR_TIME remain accepted provenance supplied by a
# Pine template, but cannot be mandatory until the deployed alert template is
# changed and verified end-to-end.
_REQUIRED_FIELDS = frozenset({"SYMBOL", "SIDE", "QTY", "ORDER_TYPE", "TIME_IN_FORCE"})
_OPTIONAL_PROVENANCE_FIELDS = frozenset({"EVENT_ID", "BAR_TIME"})
_EXECUTABLE_FIELDS = _REQUIRED_FIELDS | _OPTIONAL_PROVENANCE_FIELDS | frozenset({
    "CANCEL_UNFILLED_AT_DEADLINE", "STOP_TRIGGER", "STOP_LIMIT", "TRAIL",
    "EXIT_PLAN", "INTERVAL", "TAKE_PROFIT",
})
# TradingView renders {{interval}} as a bare number for minutes ("1", "5"),
# and a suffixed form for anything coarser ("1H", "D", "W"). Both are
# accepted; a value that is neither is refused rather than guessed at,
# because the interval sets the bar size the runner trails on and a wrong
# guess trails a 1h signal on 1m bars.
_INTERVAL = re.compile(r"^(?:\d+[smhSMH]?|[DWMdwm])$")
_IGNORABLE_FIELDS = frozenset({"REQUIRED_ACTIONS"})
# Freshness, matching the 180s the JSON path has always used, with the same
# 30s tolerance for a clock running slightly ahead.
MAX_ALERT_AGE_SECONDS = 180
MAX_ALERT_FUTURE_SECONDS = 30
_ALLOWED_ORDER_TYPES = frozenset({"market"})
_EXECUTABLE_FLAGS = frozenset({"PLACE_PROTECTIVE_STOP_AFTER_FILL"})
# The parser stays independent of runtime allowlists. These are the current
# canonical spellings emitted by the TradingView strategies; the risk layer
# still decides whether a symbol is allowed to trade.
_CANONICAL_CRYPTO = {"BTCUSD": "BTC/USD", "ETHUSD": "ETH/USD", "ETHBTC": "ETH/BTC"}


@dataclass(frozen=True)
class PineOrderCommand:
    event_id: str | None
    bar_time: datetime | None
    symbol: str
    side: str
    qty: Decimal
    order_type: str
    time_in_force: str
    cancel_unfilled_at_deadline: bool
    place_protective_stop_after_fill: bool
    stop_trigger: Decimal | None
    stop_limit: Decimal | None
    trail: Decimal | None
    take_profit: Decimal | None = None
    exit_plan: str | None = None
    interval: str | None = None


def parse_pine_alert(content: str) -> PineOrderCommand:
    """Parse only the executable order fields from a Pine alert.

    Only explicitly documented non-executable fields are ignored. Unknown
    fields fail closed so a typo cannot silently remove protection.
    """
    if not isinstance(content, str):
        raise AlertParseError("alert must be text")
    if content.count(_PREFIX) != 1:
        raise AlertParseError("alert must contain exactly one execution prefix")
    prefix_start = content.index(_PREFIX)
    prefix_end = prefix_start + len(_PREFIX)
    if prefix_start > 0 and content[prefix_start - 1] not in " \t\r\n":
        raise AlertParseError("missing EXECUTE_ALPACA_ORDER execution prefix")
    if prefix_end < len(content) and content[prefix_end] not in "| \t\r\n":
        raise AlertParseError("missing EXECUTE_ALPACA_ORDER execution prefix")
    parts = [part.strip() for part in content[prefix_start:].split("|")]
    if not parts or parts[0] != _PREFIX:
        raise AlertParseError("missing EXECUTE_ALPACA_ORDER execution prefix")

    fields: dict[str, str] = {}
    flags: set[str] = set()
    for part in parts[1:]:
        if not part:
            continue
        if "=" in part:
            key, value = part.split("=", 1)
            key = key.strip().upper()
            if key not in _EXECUTABLE_FIELDS and key not in _IGNORABLE_FIELDS:
                raise AlertParseError(f"unrecognised field: {key}")
            if key in _EXECUTABLE_FIELDS and key in fields:
                raise AlertParseError(f"duplicate executable field: {key}")
            fields[key] = value.strip()
        else:
            flag = part.upper()
            if flag not in _EXECUTABLE_FLAGS and flag != "DO_NOT_SUMMARIZE_OR_REPOST_BEFORE_BROKER_CALL":
                raise AlertParseError(f"unrecognised field: {flag}")
            if flag in _EXECUTABLE_FLAGS and flag in flags:
                raise AlertParseError(f"duplicate executable flag: {flag}")
            flags.add(flag)

    missing = sorted(key for key in _REQUIRED_FIELDS if not fields.get(key))
    if missing:
        raise AlertParseError(f"missing required fields: {', '.join(missing)}")

    event_id = fields.get("EVENT_ID") or None
    if event_id is not None and len(event_id) > 256:
        raise AlertParseError("EVENT_ID must be 1-256 characters")
    bar_time_raw = fields.get("BAR_TIME")
    bar_time = parse_bar_time(bar_time_raw) if bar_time_raw else None
    if bar_time is not None:
        _check_freshness(bar_time)

    symbol = _CANONICAL_CRYPTO.get(fields["SYMBOL"].upper(), fields["SYMBOL"].upper())
    crypto_symbol = is_crypto(symbol)
    side = fields["SIDE"].lower()
    if side not in {"buy", "sell"}:
        raise AlertParseError("SIDE must be BUY or SELL")
    qty = _positive_decimal(fields["QTY"], "qty")
    order_type = fields["ORDER_TYPE"].lower()
    if order_type not in _ALLOWED_ORDER_TYPES:
        raise AlertParseError("ORDER_TYPE must be MARKET")
    time_in_force = fields["TIME_IN_FORCE"].lower()
    allowed_time_in_force = {"gtc", "ioc"} if crypto_symbol else {"day", "gtc", "ioc", "fok", "opg", "cls"}
    if time_in_force not in allowed_time_in_force:
        raise AlertParseError("TIME_IN_FORCE is not supported")

    protective_stop = "PLACE_PROTECTIVE_STOP_AFTER_FILL" in flags
    exit_plan = (fields.get("EXIT_PLAN") or "").upper() or None
    take_profit_raw = fields.get("TAKE_PROFIT")
    if exit_plan == "OCO_AFTER_FILL":
        if protective_stop:
            raise AlertParseError("OCO_AFTER_FILL cannot be combined with protective stop")
        # Not refused for crypto. Alpaca has no native OCO there, but the plan
        # is still available — the gateway manages the pair itself, the same
        # way DYNAMIC_TRAIL already does. Rejecting at the parser would have
        # made one plan name mean "works" on QQQ and "refused" on BTC/USD,
        # which is a property of Alpaca's API leaking into the strategy.
        #
        # TAKE_PROFIT is therefore required only where it is the ONLY way to
        # price the target: a native OCO leg is an absolute price. The managed
        # path derives it from the plan's R-multiple in config, which is where
        # Wei asked the numbers to live, and an absolute price in an alert goes
        # stale — a four-hour-old stop level inverted on us last night.
        if not take_profit_raw:
            raise AlertParseError(
                "OCO_AFTER_FILL requires TAKE_PROFIT: this plan takes explicit "
                "stop and take-profit prices, not an R-multiple")
        if not fields.get("STOP_TRIGGER"):
            raise AlertParseError("OCO_AFTER_FILL requires STOP_TRIGGER")
    elif take_profit_raw and not exit_plan:
        # Allowed with ANY plan, not just OCO_AFTER_FILL. On a ladder it prices
        # the first rung explicitly and the rest still derive from R.
        #
        # This exists for testability as much as for trading. Six live runs
        # produced six correct arms and not one rung, because every one of them
        # needed the market to travel a set distance inside a window nobody
        # controlled. A target the strategy names outright can be placed where
        # it will fire, which turns "wait and hope" into an actual experiment —
        # and the sequence it exercises (reserve, resize the stop BEFORE
        # selling, sell, route the fill, move to breakeven) is the same one
        # that runs in production.
        raise AlertParseError(
            "TAKE_PROFIT requires an EXIT_PLAN; there is nothing to apply it to")
    trigger_raw, limit_raw = fields.get("STOP_TRIGGER"), fields.get("STOP_LIMIT")
    if protective_stop and (not trigger_raw or not limit_raw):
        raise AlertParseError("PLACE_PROTECTIVE_STOP_AFTER_FILL requires STOP_TRIGGER and STOP_LIMIT")
    if not protective_stop and exit_plan != "OCO_AFTER_FILL" and (trigger_raw or limit_raw):
        raise AlertParseError("STOP_TRIGGER and STOP_LIMIT require PLACE_PROTECTIVE_STOP_AFTER_FILL")
    stop_trigger = _positive_decimal(trigger_raw, "STOP_TRIGGER") if trigger_raw else None
    stop_limit = (None if (not limit_raw or limit_raw.upper() == "NONE")
                  else _positive_decimal(limit_raw, "STOP_LIMIT"))
    take_profit = _positive_decimal(take_profit_raw, "TAKE_PROFIT") if take_profit_raw else None
    if take_profit is not None and trigger_raw:
        stop_for_check = _positive_decimal(trigger_raw, "STOP_TRIGGER")
        # The only direction check available before a fill price exists. A
        # take-profit below the stop is not a tight target, it is the pair
        # inverted — and it would arm, sit there, and never make sense.
        if side == "buy" and take_profit <= stop_for_check:
            raise AlertParseError(
                f"TAKE_PROFIT {take_profit} is at or below STOP_TRIGGER "
                f"{stop_for_check} on a BUY; the exit pair is inverted")
    if protective_stop and stop_limit is None:
        raise AlertParseError("PLACE_PROTECTIVE_STOP_AFTER_FILL requires a numeric STOP_LIMIT")
    if (exit_plan == "OCO_AFTER_FILL" and stop_limit is not None
            and stop_trigger is not None and stop_limit > stop_trigger):
        raise AlertParseError("OCO_AFTER_FILL requires STOP_LIMIT <= STOP_TRIGGER")
    if stop_trigger is not None and stop_limit is not None:
        if side == "buy" and stop_limit > stop_trigger:
            raise AlertParseError(
                "protective BUY stop-limit requires STOP_LIMIT <= STOP_TRIGGER")
        if side == "sell" and stop_limit < stop_trigger:
            raise AlertParseError(
                "protective SELL stop-limit requires STOP_LIMIT >= STOP_TRIGGER")

    interval = fields.get("INTERVAL") or None
    if interval is not None and not _INTERVAL.match(interval):
        raise AlertParseError(
            f"INTERVAL {interval!r} is not a TradingView interval; the runner's "
            "trail has no bar size without it")
    if exit_plan and exit_plan != "OCO_AFTER_FILL" and interval is None:
        raise AlertParseError(
            "EXIT_PLAN requires INTERVAL — \"previous completed bar low\" has no "
            "meaning without a bar size. Send INTERVAL={{interval}}")

    trail_raw = fields.get("TRAIL", "NONE")
    trail = None if trail_raw.upper() == "NONE" else _positive_decimal(trail_raw, "trail")
    if crypto_symbol and trail is not None:
        raise AlertParseError("TRAIL is unsupported for crypto orders on Alpaca")
    cancel_raw = fields.get("CANCEL_UNFILLED_AT_DEADLINE", "NO").upper()
    if cancel_raw not in {"YES", "NO"}:
        raise AlertParseError("CANCEL_UNFILLED_AT_DEADLINE must be YES or NO")
    return PineOrderCommand(
        event_id=event_id,
        bar_time=bar_time,
        symbol=symbol,
        side=side,
        qty=qty,
        order_type=order_type,
        time_in_force=time_in_force,
        cancel_unfilled_at_deadline=cancel_raw == "YES",
        place_protective_stop_after_fill=protective_stop,
        stop_trigger=stop_trigger,
        stop_limit=stop_limit,
        trail=trail,
        take_profit=take_profit,
        exit_plan=exit_plan,
        interval=interval,
    )


def parse_bar_time(raw: str) -> datetime:
    """Parse BAR_TIME, tolerantly but never silently.

    Nobody has yet captured what TradingView renders for {{time}} or
    {{timenow}}, so several plausible spellings are accepted rather than one
    guessed. What is NOT acceptable is treating an unreadable timestamp as
    "no timestamp" and proceeding: that is how the freshness check the JSON
    path inherited quietly stopped existing here.

    A unix epoch is accepted because TradingView's {{time}} renders as one in
    some alert contexts, and reading 1754665800 as a year would be worse than
    refusing it.
    """
    text = (raw or "").strip()
    if not text:
        raise AlertParseError("BAR_TIME is required")

    if text.isdigit():
        seconds = int(text)
        # Milliseconds if it is far past any plausible epoch second.
        if seconds > 10_000_000_000:
            seconds //= 1000
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise AlertParseError(f"BAR_TIME is not a valid timestamp: {raw!r}") from exc

    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise AlertParseError(
            f"BAR_TIME is not a recognised timestamp: {raw!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _check_freshness(bar_time: datetime, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    age = (now - bar_time).total_seconds()
    if age > MAX_ALERT_AGE_SECONDS:
        raise AlertParseError(
            f"BAR_TIME is stale: {age:.0f}s old, limit {MAX_ALERT_AGE_SECONDS}s")
    if age < -MAX_ALERT_FUTURE_SECONDS:
        raise AlertParseError(
            f"BAR_TIME is in the future by {-age:.0f}s; check the clock")


def _positive_decimal(value: str | None, name: str) -> Decimal:
    try:
        parsed = Decimal(value or "")
    except InvalidOperation as exc:
        raise AlertParseError(f"{name} must be a positive decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise AlertParseError(f"{name} must be positive")
    return parsed
