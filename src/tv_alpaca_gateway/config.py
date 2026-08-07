from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    paper_trading: bool = True
    trading_enabled: bool = False
    webhook_secret: str = ""
    allowed_symbols: frozenset[str] = frozenset({"QQQ"})
    max_qty: int = 1
    max_notional: float = 1000.0
    max_price_deviation: float = 0.05
    max_alert_age_seconds: int = 180
    db_path: Path = Path("gateway.sqlite3")
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_key_id: str = ""
    alpaca_secret_key: str = ""
    market_data_feed: str = "iex"
    market_symbols: tuple[str, ...] = ("QQQ",)
    stream_enabled: bool = False
    discord_webhook_url: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        symbols = frozenset(
            s.strip().upper()
            for s in os.getenv("ALLOWED_SYMBOLS", "QQQ").split(",")
            if s.strip()
        )
        return cls(
            paper_trading=_bool_env("PAPER_TRADING", True),
            trading_enabled=_bool_env("TRADING_ENABLED", False),
            webhook_secret=os.getenv("TV_WEBHOOK_SECRET", ""),
            allowed_symbols=symbols,
            max_qty=int(os.getenv("MAX_QTY", "1")),
            max_notional=float(os.getenv("MAX_NOTIONAL", "1000")),
            max_price_deviation=float(os.getenv("MAX_PRICE_DEVIATION", "0.05")),
            max_alert_age_seconds=int(os.getenv("MAX_ALERT_AGE_SECONDS", "180")),
            db_path=Path(os.getenv("GATEWAY_DB_PATH", "gateway.sqlite3")),
            alpaca_base_url=os.getenv(
                "ALPACA_PAPER_BASE_URL", "https://paper-api.alpaca.markets"
            ),
            alpaca_key_id=os.getenv("ALPACA_API_KEY_ID", ""),
            alpaca_secret_key=os.getenv("ALPACA_API_SECRET_KEY", ""),
            market_data_feed=os.getenv("ALPACA_MARKET_DATA_FEED", "iex").strip().lower(),
            market_symbols=tuple(
                s.strip().upper()
                for s in os.getenv("MARKET_SYMBOLS", "QQQ").split(",")
                if s.strip()
            ),
            stream_enabled=_bool_env("ALPACA_STREAM_ENABLED", False),
            discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", ""),
        )

    def validate(self) -> None:
        if not self.paper_trading:
            raise ValueError("This implementation is paper-only: PAPER_TRADING must be true")
        try:
            parsed = urlsplit(self.alpaca_base_url)
        except ValueError as exc:
            raise ValueError("ALPACA_PAPER_BASE_URL is not a valid URL") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname != "paper-api.alpaca.markets"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("Only Alpaca's paper API URL is permitted")
        if self.max_qty < 1 or self.max_notional <= 0 or not 0 < self.max_price_deviation <= 1:
            raise ValueError("Risk limits must be positive")
        if not self.allowed_symbols:
            raise ValueError("ALLOWED_SYMBOLS cannot be empty")
        if self.market_data_feed not in {"iex", "sip", "delayed_sip"}:
            raise ValueError("ALPACA_MARKET_DATA_FEED must be iex, sip, or delayed_sip")

    @property
    def market_stream_url(self) -> str:
        return f"wss://stream.data.alpaca.markets/v2/{self.market_data_feed}"

    @property
    def trade_stream_url(self) -> str:
        return f"{self.alpaca_base_url.replace('https://', 'wss://').rstrip('/')}/stream"


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
