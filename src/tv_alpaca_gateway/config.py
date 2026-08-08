from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    paper_trading: bool = True
    trading_enabled: bool = False
    webhook_secret: str = ""
    allowed_symbols: frozenset[str] = frozenset({"QQQ"})
    max_qty: int = 1
    # Crypto is sized separately because one MAX_QTY cannot serve both: 1 is a
    # sane share count and an absurd amount of BTC, while 0.001 is sane BTC and
    # an invalid share count for a stop order. Zero means crypto is not enabled
    # for trading, which is the default.
    crypto_max_qty: Decimal = Decimal("0")
    max_notional: float = 1000.0
    max_price_deviation: float = 0.05
    max_alert_age_seconds: int = 180
    db_path: Path = Path("gateway.sqlite3")
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_key_id: str = ""
    alpaca_secret_key: str = ""
    market_data_feed: str = "iex"
    market_symbols: tuple[str, ...] = ("QQQ",)
    crypto_symbols: tuple[str, ...] = ()
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
            crypto_max_qty=_decimal_env("CRYPTO_MAX_QTY", Decimal("0")),
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
            crypto_symbols=tuple(
                t.strip().upper()
                for t in os.getenv("CRYPTO_SYMBOLS", "").split(",")
                if t.strip()
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
        if self.crypto_max_qty < 0:
            raise ValueError("CRYPTO_MAX_QTY cannot be negative")
        # A crypto symbol in the allowlist with no size is a trap: the alert
        # passes the allowlist and is then rejected on quantity, which reads as
        # a risk-limit problem rather than missing configuration.
        crypto_allowed = [s for s in self.allowed_symbols if "/" in s]
        if crypto_allowed and self.crypto_max_qty <= 0:
            raise ValueError(
                f"CRYPTO_MAX_QTY must be set to trade {', '.join(sorted(crypto_allowed))}")
        if any("/" not in t for t in self.crypto_symbols):
            raise ValueError("CRYPTO_SYMBOLS must use the slash form, e.g. BTC/USD")
        # The two lists are deliberately NOT merged: each names one endpoint,
        # and which socket a symbol belongs on is configuration, not inference.
        #
        # Enforced rather than merely documented because the failure is silent
        # and disproportionate. Alpaca answers a crypto pair on the equity
        # endpoint with {"T":"error","code":400,"msg":"invalid syntax"} and
        # rejects the WHOLE subscription, so one misplaced symbol takes the
        # equity feed down with it and then reconnects forever behind a warning.
        misplaced = sorted(t for t in self.market_symbols if "/" in t)
        if misplaced:
            raise ValueError(
                f"MARKET_SYMBOLS is for equities; move {', '.join(misplaced)} "
                f"to CRYPTO_SYMBOLS")
        if not self.allowed_symbols:
            raise ValueError("ALLOWED_SYMBOLS cannot be empty")
        if self.market_data_feed not in {"iex", "sip", "delayed_sip"}:
            raise ValueError("ALPACA_MARKET_DATA_FEED must be iex, sip, or delayed_sip")

    @property
    def market_stream_url(self) -> str:
        return f"wss://stream.data.alpaca.markets/v2/{self.market_data_feed}"

    @property
    def crypto_stream_url(self) -> str:
        """Crypto is a different endpoint, not a different feed of the same one.

        Same handshake dialect, so the market socket works unchanged — but the
        equity URL carries no crypto and returns nothing for BTC/USD.
        """
        return "wss://stream.data.alpaca.markets/v1beta3/crypto/us"

    @property
    def trade_stream_url(self) -> str:
        return f"{self.alpaca_base_url.replace('https://', 'wss://').rstrip('/')}/stream"


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _decimal_env(name: str, default: Decimal) -> Decimal:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return Decimal(raw.strip())
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a decimal number, got {raw!r}") from exc
