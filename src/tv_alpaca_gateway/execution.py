from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from . import assets
from .config import Settings
from .pine_alert_parser import PineOrderCommand
from .store import EventStore


@dataclass(frozen=True)
class ExecutionResult:
    entry_order_id: str | None
    protection_order_id: str | None = None
    entry_status: str | None = None


def _command_id(command: PineOrderCommand) -> str:
    payload = {
        "symbol": command.symbol,
        "side": command.side,
        "qty": str(command.qty),
        "order_type": command.order_type,
        "time_in_force": command.time_in_force,
        "cancel_unfilled_at_deadline": command.cancel_unfilled_at_deadline,
        "place_protective_stop_after_fill": command.place_protective_stop_after_fill,
        "stop_trigger": str(command.stop_trigger) if command.stop_trigger is not None else None,
        "stop_limit": str(command.stop_limit) if command.stop_limit is not None else None,
        "trail": str(command.trail) if command.trail is not None else None,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return f"pine-exec-{digest}"


def _as_decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def execute_pine_command(
    command: PineOrderCommand,
    settings: Settings,
    broker: Any,
    store: EventStore,
    *,
    deadline_seconds: float = 60,
) -> ExecutionResult:
    """Submit one parsed command and reconcile protection from broker state.

    This function deliberately accepts a narrow broker protocol supplied by the
    execution contract: ``latest_trade_price``, ``submit_order``,
    ``position_qty``, and ``cancel_order``. It never derives held quantity from
    the entry fill, because crypto fees are deducted in kind.
    """
    settings.validate()
    if not settings.trading_enabled:
        return ExecutionResult(None, entry_status="kill_switch")

    symbol = assets.resolve(command.symbol, settings.allowed_symbols)
    if symbol not in settings.allowed_symbols:
        raise ValueError("symbol is not allowlisted")

    crypto = assets.is_crypto(symbol)
    configured_max = settings.crypto_max_qty if crypto else Decimal(settings.max_qty)
    if command.qty > configured_max:
        raise ValueError("requested quantity exceeds configured limit")
    if configured_max <= 0:
        raise ValueError("no order size configured")

    reference_price = _as_decimal(broker.latest_trade_price(symbol))
    if command.qty * reference_price > _as_decimal(settings.max_notional):
        raise ValueError("notional exceeds configured limit")

    event_id = _command_id(command)
    if not store.claim(event_id):
        return ExecutionResult(None, entry_status="duplicate")

    entry_kwargs = {
        "symbol": symbol,
        "qty": command.qty,
        "side": command.side,
        "type": "market",
        "time_in_force": command.time_in_force,
        "client_order_id": event_id,
    }
    try:
        entry = broker.submit_order(**entry_kwargs)
    except Exception as exc:
        store.update(event_id, "failed", str(exc))
        store.release(event_id)
        raise

    entry_id = str(entry["id"])
    entry_status = str(entry.get("status", "unknown"))
    store.update(event_id, f"broker_{entry_status}", broker_order_id=entry_id)

    if entry_status != "filled":
        if command.cancel_unfilled_at_deadline and deadline_seconds <= 0:
            broker.cancel_order(entry_id)
            store.update(event_id, "broker_canceled", "canceled at deadline")
        return ExecutionResult(entry_id, entry_status=entry_status)

    if not command.place_protective_stop_after_fill:
        return ExecutionResult(entry_id, entry_status=entry_status)

    # This is intentionally a broker read, not filled_qty arithmetic. Alpaca
    # deducts crypto fees from the received asset, so the held position can be
    # smaller than the reported fill.
    held_qty = _as_decimal(broker.position_qty(symbol))
    if held_qty <= 0:
        return ExecutionResult(entry_id, entry_status=entry_status)

    protection_kwargs: dict[str, Any] = {
        "symbol": symbol,
        "qty": held_qty,
        "side": "sell" if command.side == "buy" else "buy",
        "time_in_force": command.time_in_force,
        "client_order_id": f"{event_id}-protection",
    }
    if crypto:
        protection_kwargs.update(
            {
                "type": "stop_limit",
                "stop_price": command.stop_trigger,
                "limit_price": command.stop_limit,
            }
        )
    elif command.trail is not None:
        protection_kwargs.update({"type": "trailing_stop", "trail_price": command.trail})
    else:
        protection_kwargs.update(
            {
                "type": "stop_limit",
                "stop_price": command.stop_trigger,
                "limit_price": command.stop_limit,
            }
        )

    try:
        protection = broker.submit_order(**protection_kwargs)
    except Exception as exc:
        store.update(event_id, "protection_failed", str(exc), broker_order_id=entry_id)
        raise
    protection_id = str(protection["id"])
    store.update(event_id, "protection_submitted", broker_order_id=entry_id)
    return ExecutionResult(entry_id, protection_id, entry_status)
