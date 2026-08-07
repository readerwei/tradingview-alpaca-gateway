from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from . import assets
from .config import Settings
from .models import Signal


class RiskError(ValueError):
    pass


@dataclass(frozen=True)
class ApprovedOrder:
    symbol: str
    side: str
    # Decimal, not int. An integer quantity makes the smallest possible BTC
    # order 1 coin (~$65,000) when Alpaca's minimum is 0.000015548 (~$1), and
    # float would reintroduce binary rounding on a number the broker parses as
    # exact decimal text.
    qty: Decimal
    limit_price: float
    time_in_force: str = "day"
    extended_hours: bool = False

    @property
    def is_crypto(self) -> bool:
        return assets.is_crypto(self.symbol)

    @property
    def notional(self) -> Decimal:
        return self.qty * Decimal(str(self.limit_price))


def approve(signal: Signal, settings: Settings,
            reference_price: float | None = None) -> ApprovedOrder:
    # An alert may say BTCUSD while the allowlist declares BTC/USD. They name
    # one asset, so resolve to the allowlisted spelling before checking it —
    # otherwise a legitimate alert is refused for a punctuation mismatch.
    symbol = assets.resolve(signal.symbol, settings.allowed_symbols)
    if symbol not in settings.allowed_symbols:
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

    crypto = assets.is_crypto(symbol)
    qty = settings.crypto_max_qty if crypto else Decimal(settings.max_qty)
    if qty <= 0:
        # Reported as configuration, not as a risk limit. Saying "notional
        # exceeds limit" here sent an operator to raise MAX_NOTIONAL, which
        # could never help, because the real cause was an unset size.
        raise RiskError(
            f"no order size configured for {symbol} "
            f"({'CRYPTO_MAX_QTY' if crypto else 'MAX_QTY'} is not set)")

    if qty * Decimal(str(signal.close)) > Decimal(str(settings.max_notional)):
        raise RiskError("notional exceeds configured limit")

    return ApprovedOrder(
        symbol=symbol,
        side=signal.action,
        qty=qty,
        limit_price=signal.close,
        # Alpaca rejects `day` on crypto, and extended hours is meaningless on
        # a market that never closes.
        time_in_force=assets.time_in_force(symbol),
        extended_hours=False,
    )
