"""Authenticated smoke test: does the handshake work against real Alpaca?

Read-only. Connects, authenticates, subscribes, listens briefly, disconnects.
Places no orders and never prints credentials.
"""
import asyncio
import os
import sys

from tv_alpaca_gateway.config import Settings
from tv_alpaca_gateway.stream import AlpacaMarketStream, AlpacaTradeUpdateStream

SECONDS = int(os.environ.get("SMOKE_SECONDS", "20"))


async def smoke(name, stream, stop):
    got = []
    task = asyncio.create_task(stream.run_forever(stop))
    await asyncio.sleep(SECONDS)
    stop.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return got


async def main():
    s = Settings.from_env()
    print(f"market  : {s.market_stream_url}")
    print(f"trading : {s.trade_stream_url}")
    print()

    # ---- 1. market data: authenticate + subscribe, count messages
    quotes, trades, errors = [], [], []
    market = AlpacaMarketStream(
        s,
        on_quote=lambda e: quotes.append(e),
        on_trade=lambda e: trades.append(e),
        on_error=lambda e: errors.append(e),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(market.run_forever(stop))
    await asyncio.sleep(SECONDS)
    stop.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    print(f"MARKET STREAM  quotes={len(quotes)} trades={len(trades)} errors={len(errors)}")
    for e in errors[:3]:
        print(f"   error: {e!r}")
    if quotes:
        q = quotes[-1]
        print(f"   last quote  {q.symbol} bid={q.bid_price} ask={q.ask_price}")
    if trades:
        t = trades[-1]
        print(f"   last trade  {t.symbol} price={t.price} size={t.size}")

    # Handshake and data flow are reported SEPARATELY. "No errors" alone called
    # a connected-but-silent socket a success — and connected-but-silent is
    # precisely how the greeting race failed, so the smoke test was blind to
    # the bug it existed to catch.
    #
    # They cannot be collapsed into one check either: equities are legitimately
    # silent outside regular hours, so demanding data would fail every night on
    # a correct system. Use --require-data when the market is known to be open,
    # or run the crypto smoke test, where silence is always suspicious.
    market_ok = not errors
    market_data = bool(quotes or trades)

    # ---- 2. trade updates: authenticate + listen only. No orders.
    up_errors, connected = [], []
    trade = AlpacaTradeUpdateStream(
        s,
        on_update=lambda e: None,
        on_error=lambda e: up_errors.append(e),
        on_connected=lambda: connected.append(True) or asyncio.sleep(0),
    )
    stop2 = asyncio.Event()
    task2 = asyncio.create_task(trade.run_forever(stop2))
    await asyncio.sleep(8)
    stop2.set()
    task2.cancel()
    try:
        await task2
    except asyncio.CancelledError:
        pass

    print(f"TRADE STREAM   connected={len(connected)} errors={len(up_errors)}")
    for e in up_errors[:3]:
        print(f"   error: {e!r}")
    trade_ok = bool(connected) and not up_errors

    require_data = "--require-data" in sys.argv
    print()
    print(f"market handshake  : {'OK' if market_ok else 'FAILED'}")
    print(f"market data flow  : "
          + ("FLOWING" if market_data else
             ("NONE — FAILED (--require-data)" if require_data else
              "none (expected outside regular hours)")))
    print(f"trading handshake : {'OK' if trade_ok else 'FAILED'}")

    ok = market_ok and trade_ok and (market_data or not require_data)
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
