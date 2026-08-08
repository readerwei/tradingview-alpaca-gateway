# TradingView → Alpaca Gateway

A small, deterministic webhook receiver for TradingView signals. It validates alerts, enforces an allowlist and risk limits, deduplicates event IDs, submits **paper-only** Alpaca orders, and optionally posts receipts to Discord.

## Safety defaults

- `PAPER_TRADING=true` is required.
- `TRADING_ENABLED=false` is the default kill switch.
- Live Alpaca URLs are rejected.
- Position quantity is determined server-side; TradingView cannot override it.
- Alert prices must be within `MAX_PRICE_DEVIATION` of Alpaca's latest trade (default 5%).
- Alerts must include a fresh timestamp and unique `event_id`.
- Duplicate event IDs are rejected atomically in SQLite.
- No broker credentials are committed; use environment variables.

This is an execution scaffold, not a claim that a strategy is profitable. Validate signals out of sample and paper trade before considering any live deployment. This repository does not enable live trading.

## Run locally

```bash
cp .env.example .env
# edit .env with paper credentials and a random webhook secret
uv sync --extra test
set -a; . ./.env; set +a
uv run uvicorn tv_alpaca_gateway.app:app --host 127.0.0.1 --port 8000
```

Health check:

```bash
curl http://127.0.0.1:8000/healthz
```

Send a test alert:

```bash
curl -X POST http://127.0.0.1:8000/webhooks/tradingview \
  -H 'content-type: application/json' \
  -H "x-tv-secret: $TV_WEBHOOK_SECRET" \
  -d '{"event_id":"demo-1","symbol":"QQQ","action":"buy","timeframe":"1m","bar_time":"2026-08-06T22:00:00Z","close":700}'
```

The service returns quickly after accepting/submitting the signal. The broker response and receipt are stored in SQLite at `GATEWAY_DB_PATH`.

## Alpaca streaming

The persistent stream is opt-in. Set `ALPACA_STREAM_ENABLED=true` only after paper credentials are configured:

```dotenv
ALPACA_STREAM_ENABLED=true
ALPACA_MARKET_DATA_FEED=iex
MARKET_SYMBOLS=QQQ,META
```

When enabled, the FastAPI lifespan starts two reconnecting WebSocket clients:

- Alpaca market data: quotes and trades for `MARKET_SYMBOLS`
- Alpaca paper trading updates: submitted, partial fills, fills, cancellations, rejections, and other order events

The clients authenticate, subscribe, reconnect with bounded exponential backoff, and stop cleanly with the application. Order updates are matched to submitted orders by broker order ID and update the SQLite event record; partial-fill and terminal-status details are also sent to the optional Discord notifier. The default remains disabled, so normal tests and local webhook use do not open network connections.

This stream is still paper-only and is not a live-trading safety certification. Before any unattended use, add a durable outbox/retry state machine, restart reconciliation, position-aware sell checks, and persisted managed-exit state.

## TradingView alert payload

Use a structured JSON message with `bar_time` generated from a **confirmed bar**. The receiver requires:

```json
{
  "event_id": "{{ticker}}-{{interval}}-{{time}}-buy",
  "symbol": "{{ticker}}",
  "action": "buy",
  "timeframe": "{{interval}}",
  "bar_time": "{{time}}",
  "close": "{{close}}"
}
```

TradingView should POST to `/webhooks/tradingview` with the shared secret in the `X-TV-Secret` header. Do not put broker credentials in the alert body.

## Crypto

Crypto is off by default. Enable it by declaring the pair in **slash form** —
that is how an asset is marked as crypto; nothing is inferred from the shape of
a ticker.

```dotenv
ALLOWED_SYMBOLS=QQQ,BTC/USD
CRYPTO_MAX_QTY=0.001        # sized separately from MAX_QTY
CRYPTO_SYMBOLS=BTC/USD      # streaming only
```

### The two symbol lists are separate. Do not mix them.

Each names a **different Alpaca endpoint**, and they are never merged:

```dotenv
MARKET_SYMBOLS=QQQ,META     # equities -> wss://stream.data.alpaca.markets/v2/<feed>
CRYPTO_SYMBOLS=BTC/USD      # crypto   -> wss://stream.data.alpaca.markets/v1beta3/crypto/us
```

Putting a crypto pair in `MARKET_SYMBOLS` is refused at startup:

```text
ValueError: MARKET_SYMBOLS is for equities; move BTC/USD to CRYPTO_SYMBOLS
```

That check exists because the runtime failure is silent and out of proportion
to the mistake. Alpaca answers a crypto pair on the equity endpoint with
`{"T":"error","code":400,"msg":"invalid syntax"}` and rejects the **entire**
subscription — so one misplaced symbol stops quotes for every equity in the
list as well, and the stream then reconnects forever behind a warning log. The
startup message names the symbol and the variable to move it to; Alpaca's does
neither.

A list may be empty. No socket is opened for one, since an empty subscription
would hold a connection open receiving nothing.

`CRYPTO_MAX_QTY` exists because one setting cannot serve both classes: `1` is a
sane share count and an absurd amount of BTC, while `0.001` is sane BTC and an
invalid share count for a stop order. Listing a pair without a size is refused
at startup rather than at the first alert.

Alerts may say `BTCUSD` or `BTC/USD`; both resolve to the allowlisted spelling.
Crypto orders use `gtc`, because Alpaca rejects `day` on them.

Two differences to know before trusting any position arithmetic:

* **Fees are charged in kind.** A filled 0.001 BTC leaves a position of
  0.0009975 — 0.25% smaller. Anything that assumes `filled_qty` equals the
  resulting position drifts by the fee on every trade.
* **The bars carry no volume.** Alpaca's crypto bars report `v: 0`, so any
  volume filter is silently vacuous.

## Tests

```bash
uv run --extra test pytest
```

### Scripts that talk to real Alpaca

Unit tests cannot prove the wire protocol is right — a mock speaks whatever the
implementation speaks. These do, and each one found a bug the suite could not.

```bash
set -a && . ~/.config/alpaca/paper.env && set +a

uv run python scripts/ticks.py                     # live quotes, tick by tick
uv run python scripts/ticks.py --symbol QQQ        # equities; routes automatically
uv run python scripts/smoke_stream.py              # equity + trading handshakes
uv run python scripts/smoke_crypto.py              # crypto handshake and pricing
```

All of the above are read-only. The end-to-end test is not — it **places one
real paper order** and refuses to run unless the account is paper and trading is
explicitly enabled:

```bash
export TRADING_ENABLED=true ALPACA_STREAM_ENABLED=true
export ALLOWED_SYMBOLS="BTC/USD" CRYPTO_SYMBOLS="BTC/USD" CRYPTO_MAX_QTY=0.001
uv run python scripts/e2e_paper.py
```

It drives webhook → risk → broker → Alpaca → `trade_updates` stream → store,
then the reconnect resync, then shutdown with live sockets. Crypto is used
because it trades 24/7, so the whole path can be exercised outside market hours.

## Architecture

```text
TradingView → HTTPS endpoint → authentication → schema/freshness checks
             → allowlist/risk gate → SQLite idempotency → Alpaca paper API
             → optional Discord receipt
```

For a deployment with no public broker webhook, use the optional Discord relay:

```text
TradingView → Discord incoming webhook → private channel
             → outbound Python Discord bot → 127.0.0.1 gateway → Alpaca paper API
```

The relay admits only messages from one configured channel and source webhook ID. It requires Discord's Message Content intent, forwards structured JSON to the local gateway, and ignores human messages, other bots, other channels, malformed content, and unapproved webhooks. Keep the bot token, source webhook URL, and broker credentials out of Git and out of Discord messages.

Run the relay separately:

```bash
uv sync --extra relay
set -a; . ./.env; set +a
uv run python -c 'from tv_alpaca_gateway.relay import run_relay; run_relay()'
```

For production, put the service behind a managed HTTPS reverse proxy, add a durable queue/worker, rotate secrets, monitor failures, and keep the kill switch accessible outside the request process. Receipt-notification failures are deliberately isolated from broker submission state.

## Managed exits (paper-only core)

`tv_alpaca_gateway.order_manager.ExitManager` coordinates multiple fixed take-profit limit orders with one trailing-stop order. It is intentionally broker-agnostic and deterministic:

```python
from decimal import Decimal
from tv_alpaca_gateway.order_manager import ExitManager, ExitPlan
from tv_alpaca_gateway.alpaca_exit_broker import AlpacaPaperExitBroker

plan = ExitPlan(
    symbol="QQQ",
    take_profits=((Decimal("725"), 3), (Decimal("730"), 3)),
    trail_percent=Decimal("2"),
)
manager = ExitManager(AlpacaPaperExitBroker(settings), plan)
manager.start(position_qty=10)  # 3 + 3 at targets, 4 under trailing stop
```

When a take-profit fill arrives from Alpaca trade updates, call `on_fill(FillEvent(...))`; the manager reduces the trailing-stop quantity. If the trailing stop fills, it cancels the unfilled take-profit orders. This is not a native Alpaca OCO relationship, so the controller must persist state, reconcile after restarts, and fail closed before any live use. The current implementation remains paper-only and is not wired to the gateway kill switch or a live WebSocket worker.
