"""Two diagnostic questions, two switches.

`LOG_LEVEL=DEBUG` was built to answer "why did nothing fire" and instead
answered "what was every tick". Wei hit the flood within minutes of it
shipping: the reasoning lines were real, present, and unfindable under
thousands of market messages — which is the same failure as not logging them.

    is data flowing?      one line a minute        INFO, always on
    what was every tick?  thousands of lines       LOG_MARKET_DATA=true
"""

from __future__ import annotations

import logging

import pytest

from tv_alpaca_gateway.config import configure_logging
from tv_alpaca_gateway.market_log import MARKET_LOGGER, MarketDataCounters
from tv_alpaca_gateway.market_log import logger as market_logger


@pytest.fixture(autouse=True)
def _restore_levels():
    names = ("", "tv_alpaca_gateway", MARKET_LOGGER)
    before = [(n, logging.getLogger(n).level) for n in names]
    yield
    for name, level in before:
        logging.getLogger(name).setLevel(level)


# ═══════════════════════════════ the two questions, answered separately

def test_debug_shows_reasoning_without_the_tick_firehose(caplog):
    """The default, and the whole point: turning on DEBUG to find out why a
    rung did not fire must not bury the answer."""
    configure_logging("DEBUG", log_market_data=False)

    with caplog.at_level(logging.DEBUG, logger="tv_alpaca_gateway"):
        logging.getLogger("tv_alpaca_gateway.exit_manager").debug("rungs tp1+190.00")
        market_logger.debug("market trade symbol=BTC/USD price=64000")

    assert "tp1+190.00" in caplog.text, "the reasoning line was lost"
    assert "market trade" not in caplog.text, "the firehose leaked into DEBUG"


def test_the_firehose_is_available_when_asked_for(caplog):
    """Off by default is not the same as gone. A diagnosis sometimes needs
    every message, and sampling would make it lossy exactly then."""
    configure_logging("DEBUG", log_market_data=True)

    with caplog.at_level(logging.DEBUG, logger="tv_alpaca_gateway"):
        market_logger.debug("market trade symbol=BTC/USD price=64000")

    assert "market trade" in caplog.text


def test_the_child_logger_is_muted_explicitly_not_by_omission():
    """It is a child of the package logger, so it INHERITS DEBUG. Relying on
    inheritance to keep it quiet would reproduce the flood."""
    configure_logging("DEBUG", log_market_data=False)

    assert logging.getLogger(MARKET_LOGGER).level == logging.WARNING
    assert logging.getLogger("tv_alpaca_gateway").level == logging.DEBUG


# ═══════════════════════════════════════════════════ the throughput summary

def test_a_subscribed_symbol_that_sent_nothing_still_gets_a_line(caplog):
    """The most valuable line here. A stream that is connected and delivering
    nothing looks exactly like a quiet market, and telling them apart took two
    days the first time."""
    counters = MarketDataCounters()
    counters.record_trade("BTC/USD")

    with caplog.at_level(logging.INFO, logger="tv_alpaca_gateway"):
        counters.emit(expected=["BTC/USD", "QQQ"])

    assert "market QQQ: 0 trades, 0 bars" in caplog.text


def test_bars_and_traded_bars_are_counted_separately(caplog):
    """On Alpaca's crypto feed a third of bars contain a trade. One combined
    number would hide the property that broke the ladder."""
    counters = MarketDataCounters()
    for i in range(10):
        counters.record_bar("BTC/USD", trade_count=1 if i < 3 else 0)

    with caplog.at_level(logging.INFO, logger="tv_alpaca_gateway"):
        counters.emit()

    assert "10 bars (3 traded)" in caplog.text


def test_counters_do_not_leak_across_windows(caplog):
    """A summary that keeps accumulating reports throughput that stopped
    minutes ago as though it were current."""
    counters = MarketDataCounters()
    counters.record_trade("BTC/USD")

    with caplog.at_level(logging.INFO, logger="tv_alpaca_gateway"):
        counters.emit()
        caplog.clear()
        counters.emit(expected=["BTC/USD"])

    assert "market BTC/USD: 0 trades" in caplog.text, "the window did not reset"


def test_counting_happens_even_when_the_firehose_is_off(caplog):
    """Counting is cheap; printing is what costs. The everyday question must
    be answerable without turning the flood on."""
    configure_logging("DEBUG", log_market_data=False)
    counters = MarketDataCounters()
    counters.record_trade("BTC/USD")
    counters.record_bar("BTC/USD", trade_count=2)

    with caplog.at_level(logging.INFO, logger="tv_alpaca_gateway"):
        counters.emit()

    assert "1 trades, 1 bars (1 traded)" in caplog.text
