from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


class AlertParseError(ValueError):
    """The alert is not an executable Pine/TradingView order command."""


_PREFIX = "EXECUTE_ALPACA_ORDER"
_REQUIRED_FIELDS = frozenset({"SYMBOL", "SIDE", "QTY", "ORDER_TYPE", "TIME_IN_FORCE"})
_EXECUTABLE_FIELDS = _REQUIRED_FIELDS | frozenset({
    "CANCEL_UNFILLED_AT_DEADLINE", "STOP_TRIGGER", "STOP_LIMIT", "TRAIL",
})
_IGNORABLE_FIELDS = frozenset({"REQUIRED_ACTIONS"})
_ALLOWED_ORDER_TYPES = frozenset({"market"})
_EXECUTABLE_FLAGS = frozenset({"PLACE_PROTECTIVE_STOP_AFTER_FILL"})
# The parser stays independent of runtime allowlists. These are the current
# canonical spellings emitted by the TradingView strategies; the risk layer
# still decides whether a symbol is allowed to trade.
_CANONICAL_CRYPTO = {"BTCUSD": "BTC/USD", "ETHUSD": "ETH/USD", "ETHBTC": "ETH/BTC"}


@dataclass(frozen=True)
class PineOrderCommand:
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

    symbol = _CANONICAL_CRYPTO.get(fields["SYMBOL"].upper(), fields["SYMBOL"].upper())
    is_crypto = symbol in _CANONICAL_CRYPTO.values()
    side = fields["SIDE"].lower()
    if side not in {"buy", "sell"}:
        raise AlertParseError("SIDE must be BUY or SELL")
    qty = _positive_decimal(fields["QTY"], "qty")
    order_type = fields["ORDER_TYPE"].lower()
    if order_type not in _ALLOWED_ORDER_TYPES:
        raise AlertParseError("ORDER_TYPE must be MARKET")
    time_in_force = fields["TIME_IN_FORCE"].lower()
    allowed_time_in_force = {"gtc", "ioc"} if is_crypto else {"day", "gtc", "ioc", "fok", "opg", "cls"}
    if time_in_force not in allowed_time_in_force:
        raise AlertParseError("TIME_IN_FORCE is not supported")

    protective_stop = "PLACE_PROTECTIVE_STOP_AFTER_FILL" in flags
    trigger_raw, limit_raw = fields.get("STOP_TRIGGER"), fields.get("STOP_LIMIT")
    if protective_stop and (not trigger_raw or not limit_raw):
        raise AlertParseError("PLACE_PROTECTIVE_STOP_AFTER_FILL requires STOP_TRIGGER and STOP_LIMIT")
    if not protective_stop and (trigger_raw or limit_raw):
        raise AlertParseError("STOP_TRIGGER and STOP_LIMIT require PLACE_PROTECTIVE_STOP_AFTER_FILL")
    stop_trigger = _positive_decimal(trigger_raw, "STOP_TRIGGER") if trigger_raw else None
    stop_limit = _positive_decimal(limit_raw, "STOP_LIMIT") if limit_raw else None
    if stop_trigger is not None and stop_limit is not None:
        if side == "buy" and stop_limit > stop_trigger:
            raise AlertParseError(
                "protective BUY stop-limit requires STOP_LIMIT <= STOP_TRIGGER")
        if side == "sell" and stop_limit < stop_trigger:
            raise AlertParseError(
                "protective SELL stop-limit requires STOP_LIMIT >= STOP_TRIGGER")

    trail_raw = fields.get("TRAIL", "NONE")
    trail = None if trail_raw.upper() == "NONE" else _positive_decimal(trail_raw, "trail")
    if is_crypto and trail is not None:
        raise AlertParseError("TRAIL is unsupported for crypto orders on Alpaca")
    cancel_raw = fields.get("CANCEL_UNFILLED_AT_DEADLINE", "NO").upper()
    if cancel_raw not in {"YES", "NO"}:
        raise AlertParseError("CANCEL_UNFILLED_AT_DEADLINE must be YES or NO")
    return PineOrderCommand(
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
    )


def _positive_decimal(value: str | None, name: str) -> Decimal:
    try:
        parsed = Decimal(value or "")
    except InvalidOperation as exc:
        raise AlertParseError(f"{name} must be a positive decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise AlertParseError(f"{name} must be positive")
    return parsed
