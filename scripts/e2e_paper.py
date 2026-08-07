"""End-to-end plumbing test on Alpaca paper, using crypto's 24/7 market.

Exercises the path no unit test can reach:

    webhook -> risk -> broker submit -> Alpaca
                    -> trade_updates stream -> store

then stales the store and runs the reconnect resync against it.

PLACES ONE REAL PAPER ORDER, sized by CRYPTO_MAX_QTY. Refuses to run unless the
account is paper and trading is explicitly enabled.

    set -a && . ~/.config/alpaca/paper.env && set +a
    export TRADING_ENABLED=true ALPACA_STREAM_ENABLED=true
    export ALLOWED_SYMBOLS="BTC/USD" CRYPTO_SYMBOLS="BTC/USD" CRYPTO_MAX_QTY=0.001
    export TV_WEBHOOK_SECRET=... GATEWAY_DB_PATH=./e2e.sqlite3
    uv run python scripts/e2e_paper.py
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

import uvicorn

from tv_alpaca_gateway.app import create_app
from tv_alpaca_gateway.broker import AlpacaPaperClient
from tv_alpaca_gateway.config import Settings

PORT = int(os.environ.get("E2E_PORT", "8123"))
SECRET = os.environ["TV_WEBHOOK_SECRET"]
OK, BAD = "\033[32mOK\033[0m", "\033[31mFAIL\033[0m"


def post_alert(symbol: str, price: float, event_id: str) -> dict:
    payload = json.dumps({
        "event_id": event_id, "symbol": symbol, "action": "buy",
        "timeframe": "1", "close": price,
        "bar_time": datetime.now(timezone.utc).isoformat(),
    }).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/webhooks/tradingview", data=payload,
        headers={"content-type": "application/json", "x-tv-secret": SECRET},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return {"http_status": exc.code, "body": exc.read().decode(errors="replace")}


async def until(predicate, timeout: float, interval: float = 0.4):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if (value := predicate()):
            return value
        await asyncio.sleep(interval)
    return None


async def main() -> None:
    settings = Settings.from_env()
    settings.validate()
    if not settings.paper_trading or "paper-api" not in settings.alpaca_base_url:
        raise SystemExit("refusing to run: not a paper account")
    if not settings.trading_enabled:
        raise SystemExit("TRADING_ENABLED must be true for the end-to-end test")

    symbol = settings.crypto_symbols[0]
    price = AlpacaPaperClient(settings).latest_trade_price(symbol)
    print(f"account : PAPER {settings.alpaca_base_url}")
    print(f"order   : buy {settings.crypto_max_qty} {symbol} @ ~{price:,.2f} "
          f"(~${float(settings.crypto_max_qty) * price:,.2f})\n")

    app = create_app(settings)
    store, stream = app.state.store, app.state.stream
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT,
                                           log_level="warning"))
    serving = asyncio.create_task(server.serve())
    await until(lambda: server.started, 20)
    await asyncio.sleep(6)          # let trade_updates authenticate
    print(f"1. gateway up, streams connected                 {OK}")

    # ---- submit -----------------------------------------------------------
    event_id = f"e2e-{datetime.now(timezone.utc):%H%M%S}"
    # Marketable limit, through the offer, so it fills instead of resting.
    result = await asyncio.to_thread(post_alert, symbol, round(price * 1.002, 2),
                                     event_id)
    order_id = result.get("order_id")
    print(f"2. webhook -> order                              "
          f"{OK if order_id else BAD}   {result}")
    if not order_id:
        server.should_exit = True
        await serving
        raise SystemExit(1)

    # ---- the STREAM should update the store, with no polling from us -------
    filled = await until(
        lambda: (s := store.status(event_id)) and s.startswith("broker_")
        and s not in {"broker_new", "broker_accepted", "broker_pending_new"},
        timeout=45)
    print(f"3. stream -> store                               "
          f"{OK if filled else BAD}   status={store.status(event_id)!r}")

    # ---- reconnect resync --------------------------------------------------
    # Stale the record to a non-terminal status, as a missed update would leave
    # it, then run the resync the trade stream fires on every (re)connect.
    store.update(event_id, "broker_accepted", "staled for the resync test")
    before = store.status(event_id)
    await stream.trade_updates.on_connected()
    after = store.status(event_id)
    print(f"4. resync corrects a missed update               "
          f"{OK if after != before else BAD}   {before!r} -> {after!r}")

    # ---- clean shutdown, the thing that used to hang -----------------------
    loop = asyncio.get_running_loop()
    started = loop.time()
    server.should_exit = True
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(serving, timeout=20)
    print(f"5. shutdown with live streams                    {OK}   "
          f"{loop.time() - started:.1f}s")


asyncio.run(main())
