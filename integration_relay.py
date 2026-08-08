"""Drive a fake Discord message through the real relay into the real gateway.

No bot token, no Discord connection, no TradingView. Everything else is the
production path: handle_message -> GatewayRelay.forward -> uvicorn -> the
dry-run route -> the parser -> SQLite.
"""
import asyncio, os, sys, threading, time
from types import SimpleNamespace

import uvicorn

os.environ.setdefault("TV_WEBHOOK_SECRET", "integration-secret")
os.environ.setdefault("ALLOWED_SYMBOLS", "QQQ,BTC/USD")
os.environ.setdefault("CRYPTO_MAX_QTY", "0.05")
os.environ.setdefault("MAX_QTY", "3")
os.environ.setdefault("MAX_NOTIONAL", "3500")
os.environ.setdefault("GATEWAY_DB_PATH", "/tmp/claude-501/-Users-wzhao/91e25b9c-764a-4785-b691-3fc7815f6638/scratchpad/integration.sqlite3")

from tv_alpaca_gateway.app import create_app
from tv_alpaca_gateway.config import Settings
from tv_alpaca_gateway.relay import GatewayRelay, RelaySettings, handle_message

PORT = 8131
CHANNEL, WEBHOOK = 1530636075947659424, 555444333
ALERT = ("EXECUTE_ALPACA_ORDER | SYMBOL=QQQ | SIDE=BUY | QTY=1 | "
         "ORDER_TYPE=MARKET | TIME_IN_FORCE=DAY | REQUIRED_ACTIONS=SUBMIT_ORDER")

settings = Settings.from_env()
server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1",
                                       port=PORT, log_level="error"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(100):
    if server.started: break
    time.sleep(0.1)
print(f"gateway up on 127.0.0.1:{PORT}")

relay_settings = RelaySettings(
    token="unused", channel_id=CHANNEL, source_webhook_id=WEBHOOK,
    internal_url=f"http://127.0.0.1:{PORT}/webhooks/tradingview/pine/dry-run",
    internal_secret=os.environ["TV_WEBHOOK_SECRET"])
relay = GatewayRelay(relay_settings)

def msg(content=ALERT, channel=CHANNEL, webhook=WEBHOOK):
    return SimpleNamespace(channel=SimpleNamespace(id=channel),
                           webhook_id=webhook, content=content)

print()
cases = [
    ("the real alert",            msg()),
    ("wrong channel",             msg(channel=CHANNEL + 1)),
    ("a human typing it",         msg(webhook=None)),
    ("ordinary chat",             msg(content="how did the open go?")),
]
for label, m in cases:
    result = handle_message(m, relay_settings, relay)
    print(f"  {label:22s} forwarded={result}")

server.should_exit = True
