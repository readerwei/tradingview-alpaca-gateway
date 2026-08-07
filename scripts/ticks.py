"""Print live Alpaca quotes and trades, tick by tick.

Read-only. Places no orders and needs only market-data permission.

    set -a && . ~/.config/alpaca/paper.env && set +a
    uv run python scripts/ticks.py                       # BTC/USD, runs until Ctrl-C
    uv run python scripts/ticks.py --symbol ETH/USD
    uv run python scripts/ticks.py --symbol QQQ          # equities, RTH only
    uv run python scripts/ticks.py --seconds 30 --trades-only

Crypto routes to /v1beta3/crypto/us and equities to /v2/<feed> automatically —
they are different endpoints, and asking the wrong one returns nothing rather
than failing, which looks exactly like a quiet market.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import sys
from datetime import datetime

from tv_alpaca_gateway import assets
from tv_alpaca_gateway.config import Settings
from tv_alpaca_gateway.stream import AlpacaMarketStream

# Colour only when writing to a terminal, so piping to a file stays clean.
_TTY = sys.stdout.isatty()
DIM = "\033[2m" if _TTY else ""
RED = "\033[31m" if _TTY else ""
GRN = "\033[32m" if _TTY else ""
CYN = "\033[36m" if _TTY else ""
BLD = "\033[1m" if _TTY else ""
OFF = "\033[0m" if _TTY else ""


def _clock(iso: str) -> str:
    """Alpaca timestamps are RFC-3339 UTC with nanoseconds; show HH:MM:SS.mmm."""
    try:
        text = iso.replace("Z", "+00:00")
        if "." in text:
            head, rest = text.split(".", 1)
            frac = "".join(c for c in rest if c.isdigit())[:6].ljust(6, "0")
            text = f"{head}.{frac}+00:00"
        return datetime.fromisoformat(text).strftime("%H:%M:%S.%f")[:-3]
    except ValueError:
        return iso[-15:]


class Ticker:
    def __init__(self, symbol: str, show_quotes: bool, show_trades: bool):
        self.symbol = symbol
        self.show_quotes = show_quotes
        self.show_trades = show_trades
        self.quotes = self.trades = 0
        self.last_mid: float | None = None

    def on_quote(self, q) -> None:
        self.quotes += 1
        if not self.show_quotes:
            return
        mid = (q.bid_price + q.ask_price) / 2
        spread = q.ask_price - q.bid_price
        bps = 1e4 * spread / q.ask_price if q.ask_price else 0.0
        # Arrow marks which way the mid moved, so direction is visible without
        # reading the numbers.
        arrow = " "
        if self.last_mid is not None:
            arrow = f"{GRN}▲{OFF}" if mid > self.last_mid else (
                f"{RED}▼{OFF}" if mid < self.last_mid else f"{DIM}·{OFF}")
        self.last_mid = mid
        print(f"{DIM}{_clock(q.timestamp)}{OFF} {CYN}Q{OFF} {arrow} "
              f"bid {q.bid_price:>12,.2f} x {q.bid_size:<10,.4f} "
              f"ask {q.ask_price:>12,.2f} x {q.ask_size:<10,.4f} "
              f"{DIM}spread{OFF} {spread:>8,.2f} {DIM}({bps:.1f} bps){OFF}",
              flush=True)

    def on_trade(self, t) -> None:
        self.trades += 1
        if not self.show_trades:
            return
        # A trade above the prevailing mid is buyer-initiated; below, seller.
        side = ""
        if self.last_mid is not None:
            side = (f"{GRN}buy {OFF}" if t.price > self.last_mid
                    else f"{RED}sell{OFF}" if t.price < self.last_mid else f"{DIM}mid {OFF}")
        print(f"{DIM}{_clock(t.timestamp)}{OFF} {BLD}T{OFF} {side} "
              f"{t.price:>12,.2f} x {t.size:<12,.6f}", flush=True)

    def on_error(self, exc) -> None:
        print(f"{RED}stream error:{OFF} {exc}", file=sys.stderr, flush=True)


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="BTC/USD",
                   help="BTC/USD for crypto, QQQ for equities")
    p.add_argument("--seconds", type=float, default=0.0, help="0 runs until Ctrl-C")
    p.add_argument("--quotes-only", action="store_true")
    p.add_argument("--trades-only", action="store_true")
    a = p.parse_args()

    symbol = assets.normalise(a.symbol)
    settings = Settings.from_env()
    if not settings.alpaca_key_id or not settings.alpaca_secret_key:
        return print("ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY must be set") or 2

    crypto = assets.is_crypto(symbol)
    url = settings.crypto_stream_url if crypto else settings.market_stream_url

    ticker = Ticker(symbol, not a.trades_only, not a.quotes_only)
    stream = AlpacaMarketStream(settings, ticker.on_quote, ticker.on_trade,
                                ticker.on_error, url=url, symbols=(symbol,),
                                label="ticks")

    print(f"{BLD}{symbol}{OFF}  {DIM}{url}{OFF}")
    print(f"{DIM}{'crypto — trades 24/7' if crypto else 'equity — regular hours only'}"
          f"; Ctrl-C to stop{OFF}\n")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    task = asyncio.create_task(stream.run_forever(stop))
    if a.seconds > 0:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=a.seconds)
        stop.set()
    else:
        await stop.wait()

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    print(f"\n{DIM}{ticker.quotes:,} quotes, {ticker.trades:,} trades{OFF}")
    return 0


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(asyncio.run(main()))
