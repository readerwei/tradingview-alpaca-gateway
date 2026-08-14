"""Market-data volume, separated from market-data reasoning.

`LOG_LEVEL=DEBUG` was made to answer "why did nothing fire" and instead
answered "what was every tick", which buried the first under thousands of
lines of the second. Wei hit it within minutes of it shipping.

They are different questions and they need different switches:

    is data flowing?      one line a minute        -> INFO, always
    what was every tick?  thousands of lines       -> LOG_MARKET_DATA=true

So per-message logging moves to its own logger, silenced by default even when
the package is at DEBUG, and a counter answers the everyday question without
printing anything per message. Counting is cheap; printing is what costs.
"""

from __future__ import annotations

import logging
import threading

# Explicitly a child of the package logger, and explicitly muted in
# configure_logging — inheritance alone would hand it DEBUG and reproduce the
# flood this module exists to stop.
MARKET_LOGGER = "tv_alpaca_gateway.marketdata"

logger = logging.getLogger(MARKET_LOGGER)
summary_logger = logging.getLogger(__name__)


class MarketDataCounters:
    """How much arrived, per symbol, since the last summary.

    Kept per event type rather than as one total: "bars arrived" and "bars
    arrived that anything traded in" are different facts, and on Alpaca's
    crypto feed the second is a third of the first. A single number would hide
    exactly the property that broke the ladder.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, dict[str, int]] = {}

    def _bump(self, symbol: str, field: str, by: int = 1) -> None:
        with self._lock:
            row = self._counts.setdefault(
                symbol, {"trades": 0, "bars": 0, "traded_bars": 0, "quotes": 0})
            row[field] += by

    def record_trade(self, symbol: str) -> None:
        self._bump(symbol, "trades")

    def record_quote(self, symbol: str) -> None:
        self._bump(symbol, "quotes")

    def record_bar(self, symbol: str, trade_count: int | None) -> None:
        self._bump(symbol, "bars")
        if trade_count:
            self._bump(symbol, "traded_bars")

    def drain(self) -> dict[str, dict[str, int]]:
        """Return the window's counts and start a new one.

        Drained rather than read, so a summary can never double-count and two
        consecutive windows cannot silently share a total.
        """
        with self._lock:
            counts, self._counts = self._counts, {}
            return counts

    def emit(self, expected: list[str] | None = None) -> None:
        """Log one line per symbol, including symbols that sent nothing.

        A subscribed symbol delivering zero is the single most valuable line
        here — it is what "the stream is connected but nothing is arriving"
        looks like, and it took two days to notice the first time.
        """
        counts = self.drain()
        for symbol in sorted(set(counts) | set(expected or [])):
            row = counts.get(symbol, {"trades": 0, "bars": 0, "traded_bars": 0, "quotes": 0})
            summary_logger.info(
                "market %s: %d trades, %d bars (%d traded), %d quotes",
                symbol, row["trades"], row["bars"], row["traded_bars"], row["quotes"])
