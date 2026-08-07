from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .models import Signal


class RiskError(ValueError):
    pass


@dataclass(frozen=True)
class ApprovedOrder:
    symbol: str
    side: str
    qty: int
    limit_price: float
    time_in_force: str = "day"
    extended_hours: bool = False


def approve(signal: Signal, settings: Settings) -> ApprovedOrder:
    if signal.symbol not in settings.allowed_symbols:
        raise RiskError("symbol is not allowlisted")
    if signal.age_seconds() < -30 or signal.age_seconds() > settings.max_alert_age_seconds:
        raise RiskError("alert timestamp is stale or too far in the future")
    qty = min(settings.max_qty, 1)
    if qty * signal.close > settings.max_notional:
        raise RiskError("notional exceeds configured limit")
    return ApprovedOrder(
        symbol=signal.symbol,
        side=signal.action,
        qty=qty,
        limit_price=signal.close,
    )
