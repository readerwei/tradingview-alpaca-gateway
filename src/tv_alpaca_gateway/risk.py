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


def approve(signal: Signal, settings: Settings, reference_price: float | None = None) -> ApprovedOrder:
    if signal.symbol not in settings.allowed_symbols:
        raise RiskError("symbol is not allowlisted")
    if signal.age_seconds() < -30 or signal.age_seconds() > settings.max_alert_age_seconds:
        raise RiskError("alert timestamp is stale or too far in the future")
    if reference_price is not None:
        if reference_price <= 0:
            raise RiskError("market price reference is invalid")
        deviation = abs(signal.close - reference_price) / reference_price
        if deviation > settings.max_price_deviation:
            raise RiskError(
                f"alert close deviates from market price by {deviation:.2%} "
                f"(limit {settings.max_price_deviation:.2%})"
            )
    qty = settings.max_qty
    if qty * signal.close > settings.max_notional:
        raise RiskError("notional exceeds configured limit")
    return ApprovedOrder(
        symbol=signal.symbol,
        side=signal.action,
        qty=qty,
        limit_price=signal.close,
    )
