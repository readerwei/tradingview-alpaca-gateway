"""Live crypto smoke test, through the real configuration path.

Read-only: connects, authenticates, subscribes, prices. Places no orders.

    export CRYPTO_SYMBOLS="BTC/USD"
    export ALLOWED_SYMBOLS="QQQ,BTC/USD"
    export CRYPTO_MAX_QTY=0.001
    set -a && . ~/.config/alpaca/paper.env && set +a
    uv run python scripts/smoke_crypto.py
"""
import asyncio
import os
from decimal import Decimal

from tv_alpaca_gateway.broker import AlpacaPaperClient
from tv_alpaca_gateway.config import Settings
from tv_alpaca_gateway.risk import approve
from tv_alpaca_gateway.models import Signal
from tv_alpaca_gateway.stream import AlpacaStreamManager
from datetime import datetime, timezone

SECONDS = int(os.environ.get("SMOKE_SECONDS", "20"))


async def main() -> None:
    settings = Settings.from_env()
    settings.validate()
    if not settings.crypto_symbols:
        raise SystemExit('CRYPTO_SYMBOLS is not set — e.g. export CRYPTO_SYMBOLS="BTC/USD"')
    symbol = settings.crypto_symbols[0]
    print(f"crypto stream : {settings.crypto_stream_url}")
    print(f"symbols       : {list(settings.crypto_symbols)}")

    # 1. REST price lookup — the collar's reference, on the crypto endpoint.
    broker = AlpacaPaperClient(settings)
    price = broker.latest_trade_price(symbol)
    print(f"latest trade  : {symbol} @ {price:,.2f}")

    # 2. Risk approval end to end, with no order sent.
    signal = Signal.parse({
        "event_id": "smoke", "symbol": symbol, "action": "buy", "timeframe": "1",
        "close": price, "bar_time": datetime.now(timezone.utc).isoformat(),
    })
    order = approve(signal, settings, reference_price=price)
    notional = order.qty * Decimal(str(order.limit_price))
    print(f"approved      : {order.side} {order.qty} {order.symbol} "
          f"tif={order.time_in_force} notional=${notional:,.2f}")

    # 3. The stream, via the manager rather than a hand-patched URL.
    quotes, trades, errors = [], [], []
    manager = AlpacaStreamManager(
        settings,
        on_quote=lambda e: quotes.append(e),
        on_trade=lambda e: trades.append(e),
        on_error=lambda e: errors.append(e),
    )
    assert manager.crypto is not None, "crypto stream was not created"
    stop = asyncio.Event()
    task = asyncio.create_task(manager.crypto.run_forever(stop))
    await asyncio.sleep(SECONDS)
    stop.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    print(f"stream        : quotes={len(quotes)} trades={len(trades)} errors={len(errors)}")
    for e in errors[:3]:
        print(f"   error: {e}")
    if quotes:
        q = quotes[-1]
        spread = q.ask_price - q.bid_price
        print(f"   last quote {q.symbol} bid={q.bid_price:,.2f} ask={q.ask_price:,.2f} "
              f"spread=${spread:,.2f} ({1e4 * spread / q.ask_price:.1f} bps)")
    # Handshake and data flow are separate results, for the same reason as in
    # smoke_stream.py — and the assumption that crypto silence is always
    # suspicious turned out to be wrong. Measured on this feed: Alpaca sends a
    # snapshot burst on subscribe and then only updates on change. One sample
    # saw 6 events in the first second and nothing for the next 74; another saw
    # nothing at all until 24s. A fixed window shorter than that cadence
    # reports a working stream as broken.
    #
    # The handshake is what this script can assert. It was measured at 0.2s to
    # connect, authenticate and subscribe, so a failure there is unambiguous.
    handshake_ok = not errors
    print()
    print(f"handshake     : {'OK' if handshake_ok else 'FAILED'}")
    print(f"data flow     : "
          + ("FLOWING" if (quotes or trades) else
             f"none in {SECONDS}s (this feed is bursty; not conclusive)"))
    if not handshake_ok:
        raise SystemExit(1)


asyncio.run(main())
