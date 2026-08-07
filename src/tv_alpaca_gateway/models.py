from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


class AlertError(ValueError):
    pass


@dataclass(frozen=True)
class Signal:
    event_id: str
    symbol: str
    action: str
    timeframe: str
    bar_time: datetime
    close: float

    @classmethod
    def parse(cls, payload: dict) -> "Signal":
        required = {"event_id", "symbol", "action", "timeframe", "bar_time", "close"}
        missing = sorted(required - payload.keys())
        if missing:
            raise AlertError(f"missing fields: {', '.join(missing)}")
        event_id = str(payload["event_id"]).strip()
        symbol = str(payload["symbol"]).strip().upper()
        action = str(payload["action"]).strip().lower()
        timeframe = str(payload["timeframe"]).strip()
        if not event_id or len(event_id) > 256:
            raise AlertError("event_id must be 1-256 characters")
        # Crypto pairs are written BTC/USD, and that is the canonical form the
        # data API returns — `isalnum()` rejected it outright, so the only
        # spelling that worked was the one Alpaca does not use itself.
        #
        # Both sides must be non-empty: "BTC/" and "/USD" name no asset, and
        # checking only that the slash-stripped remainder is alphanumeric lets
        # them through.
        if len(symbol) > 12:
            raise AlertError("invalid symbol")
        parts = symbol.split("/")
        if len(parts) > 2 or not all(p.isalnum() for p in parts):
            raise AlertError("invalid symbol")
        if action not in {"buy", "sell"}:
            raise AlertError("action must be buy or sell")
        try:
            bar_time = datetime.fromisoformat(str(payload["bar_time"]).replace("Z", "+00:00"))
            close = float(payload["close"])
        except (TypeError, ValueError) as exc:
            raise AlertError("invalid bar_time or close") from exc
        if bar_time.tzinfo is None:
            raise AlertError("bar_time must include a timezone")
        if close <= 0:
            raise AlertError("close must be positive")
        return cls(event_id, symbol, action, timeframe, bar_time.astimezone(timezone.utc), close)

    def age_seconds(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (now - self.bar_time).total_seconds()
