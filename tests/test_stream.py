import asyncio

from tv_alpaca_gateway.config import Settings
from tv_alpaca_gateway.stream import (
    AlpacaStreamManager,
    MarketQuote,
    MarketTrade,
    OrderUpdate,
    parse_market_message,
    parse_order_update,
)


def test_parse_market_quote_and_trade():
    quote = parse_market_message(
        {"T": "q", "S": "QQQ", "t": "2026-08-07T14:30:00Z", "bp": 500.1, "bs": 10, "ap": 500.2, "as": 20}
    )
    trade = parse_market_message(
        {"T": "t", "S": "QQQ", "t": "2026-08-07T14:30:01Z", "p": 500.15, "s": 5, "i": 42}
    )
    assert isinstance(quote, MarketQuote)
    assert (quote.symbol, quote.bid_price, quote.ask_size) == ("QQQ", 500.1, 20)
    assert isinstance(trade, MarketTrade)
    assert (trade.symbol, trade.price, trade.trade_id) == ("QQQ", 500.15, 42)


def test_parse_order_updates_distinguishes_partial_fill_and_terminal_status():
    partial = parse_order_update(
        {
            "stream": "trade_updates",
            "data": {
                "event": "partial_fill",
                "order": {
                    "id": "order-1",
                    "client_order_id": "tv-event-1",
                    "symbol": "QQQ",
                    "side": "buy",
                    "status": "partially_filled",
                    "qty": "10",
                    "filled_qty": "4",
                },
            },
        }
    )
    rejected = parse_order_update(
        {
            "stream": "trade_updates",
            "data": {
                "event": "rejected",
                "order": {
                    "id": "order-2",
                    "client_order_id": "tv-event-2",
                    "symbol": "QQQ",
                    "side": "buy",
                    "status": "rejected",
                    "qty": "1",
                    "filled_qty": "0",
                },
            },
        }
    )
    assert isinstance(partial, OrderUpdate)
    assert partial.is_partial_fill is True
    assert partial.is_fill is True
    assert partial.is_terminal is False
    assert isinstance(rejected, OrderUpdate)
    assert rejected.is_terminal is True
    assert parse_order_update({"stream": "success", "msg": "listening"}) is None


def test_manager_starts_and_stops_both_stream_tasks(monkeypatch):
    async def scenario():
        settings = Settings(alpaca_key_id="key", alpaca_secret_key="secret", stream_enabled=True)
        manager = AlpacaStreamManager(settings)
        started = []

        async def fake_market(stop_event):
            started.append("market")
            await stop_event.wait()

        async def fake_trade(stop_event):
            started.append("trade")
            await stop_event.wait()

        monkeypatch.setattr(manager.market, "run_forever", fake_market)
        monkeypatch.setattr(manager.trade_updates, "run_forever", fake_trade)
        await manager.start()
        await asyncio.sleep(0)
        assert sorted(started) == ["market", "trade"]
        await manager.stop()
        assert manager.tasks == []

    asyncio.run(scenario())
