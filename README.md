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

Send a **fresh, non-executing** test alert (the default `TRADING_ENABLED=false` kill switch remains in force):

```bash
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EVENT_ID="manual-test-$(date +%s)"
# Set CLOSE to a recent QQQ price. It must be within MAX_PRICE_DEVIATION
# (5% by default) of Alpaca's current reference price.
CLOSE=700

curl -X POST http://127.0.0.1:8000/webhooks/tradingview \
  -H 'content-type: application/json' \
  -H "x-tv-secret: $TV_WEBHOOK_SECRET" \
  -d "{\"event_id\":\"$EVENT_ID\",\"symbol\":\"QQQ\",\"action\":\"buy\",\"timeframe\":\"1m\",\"bar_time\":\"$NOW\",\"close\":$CLOSE}"
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

## Pine command parser (current TradingView alert format)

The repository includes `parse_pine_alert()` for the current pipe-delimited Pine command format:

```text
EXECUTE_ALPACA_ORDER | SYMBOL=BTCUSD | SIDE=BUY | QTY=0.001 | ORDER_TYPE=MARKET | TIME_IN_FORCE=GTC | CANCEL_UNFILLED_AT_DEADLINE=YES | PLACE_PROTECTIVE_STOP_AFTER_FILL | STOP_TRIGGER=65000 | STOP_LIMIT=64950 | TRAIL=NONE
```

It parses and validates `SYMBOL`, `SIDE`, `QTY`, `ORDER_TYPE`, `TIME_IN_FORCE`, `CANCEL_UNFILLED_AT_DEADLINE`, the protective-stop flag, `STOP_TRIGGER`, `STOP_LIMIT`, and `TRAIL`. The current parser accepts only `ORDER_TYPE=MARKET`; it will not represent a non-market entry until the contract gains explicit entry-price fields. `BTCUSD` is normalized to `BTC/USD`; `TRAIL=NONE` becomes no trail, while `TRAIL=250` means a $250 trail distance. The parser deliberately ignores non-executable instruction fields such as `REQUIRED_ACTIONS` and `DO_NOT_SUMMARIZE_OR_REPOST_BEFORE_BROKER_CALL`.

The parser is wired into the authenticated execution route, `POST /webhooks/tradingview/pine/submit`. That route delegates to the single `execute_pine_command()` engine, which performs risk checks, idempotency claiming, entry submission, fill monitoring, protection, and optional managed-exit setup. The dry-run endpoint remains available for parse-only validation and never calls risk approval, an Alpaca client, order submission, fill monitoring, cancellation, or protection logic.

```bash
curl --data-binary @alert.txt \
  -H 'content-type: text/plain' \
  -H "x-tv-secret: $TV_WEBHOOK_SECRET" \
  http://127.0.0.1:8000/webhooks/tradingview/pine/dry-run
```

The endpoint accepts raw request bytes (the example sends `text/plain`) and rejects bodies over 4,096 bytes with HTTP 413 before decoding or parsing. A successful response contains `dry_run: true`, an audit ID, and the normalized command. This is the safe way to verify that an actual Pine alert reaches the parser without placing an order.

## JSON webhook payload (existing FastAPI route)

Use a structured JSON message with `bar_time` generated from a **confirmed bar**. All six fields below are required; unknown extra fields are ignored.

| Field | Required value | Validation / use |
|---|---|---|
| `event_id` | Unique string, 1–256 characters | SQLite idempotency key. Do not reuse it after a rejection, submission, or test. |
| `symbol` | Uppercase equity ticker or crypto pair | Must be in `ALLOWED_SYMBOLS`; crypto may be `BTCUSD` or `BTC/USD`, but the allowlist uses slash form. |
| `action` | `buy` or `sell` | Order direction. |
| `timeframe` | Non-empty strategy timeframe, e.g. `1m` | Preserved with the signal; it is not an order interval. |
| `bar_time` | ISO-8601 timestamp **with timezone**, e.g. `2026-08-07T20:00:00Z` | Must be no more than `MAX_ALERT_AGE_SECONDS` old (default 180 seconds) and no more than 30 seconds in the future. |
| `close` | Positive JSON number | Must be within `MAX_PRICE_DEVIATION` of Alpaca's current reference price (default 5%). |

```json
{
  "event_id": "{{ticker}}-{{interval}}-{{time}}-buy",
  "symbol": "{{ticker}}",
  "action": "buy",
  "timeframe": "{{interval}}",
  "bar_time": "{{time}}",
  "close": {{close}}
}
```

A minimal relay test therefore needs a **new event ID**, a current timestamp, and a plausible current price. For example (replace the timestamp and close before posting):

```json
{
  "event_id": "qqq-test-20260807T200000Z",
  "symbol": "QQQ",
  "action": "buy",
  "timeframe": "1m",
  "bar_time": "2026-08-07T20:00:00Z",
  "close": 720.00
}
```

TradingView should POST to `/webhooks/tradingview` with the shared secret in the `X-TV-Secret` header. Do not put broker credentials in the alert body.

## ⚠️ The submit route is not production-ready

`POST /webhooks/tradingview/pine/submit` places real orders and **waits
synchronously for the fill deadline** — up to `deadline_seconds`, 60 by default.

The engine runs in a worker thread, so the event loop stays responsive and other
requests are served. **That does not make this request asynchronous.** The caller
waits.

TradingView's webhook client has its own timeout. If it gives up before the
gateway answers, it may retry — and a retry is a second HTTP request carrying the
same alert.

**What protects you, and what does not:**

* `EVENT_ID` in the alert **does**. The same firing retried any number of times
  resolves to one idempotency key and one order.
* The Discord snowflake fallback **does not**. Each redelivered message can
  arrive with a new snowflake, so a retry looks like a new firing and places a
  second order.

So while this route is synchronous:

```dotenv
# required for retry safety, not optional
EVENT_ID={{ticker}}-{{interval}}-{{time}}
```

Treat this as a reviewed, paper-only integration milestone. **Do not connect a
live TradingView alert to it**, and do not treat it as production-safe, until the
background-task lifecycle replaces the in-request wait. The follow-up should
return the entry id immediately and reconcile the fill out of band.

## Dynamic managed exits

The named `DYNAMIC_TRAIL` plan is paper-only and must be requested explicitly in
the Pine alert:

```text
EXIT_PLAN=DYNAMIC_TRAIL | INTERVAL=1m | EVENT_ID={{ticker}}-{{interval}}-{{time}}
```

The plan is deterministic and anchored to the actual entry fill and original
disaster stop:

```text
TP1:       +1.2R, sell 20%
TP2:       +2.5R, sell 30%
Runner:    remaining 50%
After TP1: move the software stop to the exact entry fill (breakeven)
```

Take-profit levels are software triggers, not resting Alpaca orders. A TP order
is submitted only when an incoming trade crosses its level. The runner trail is
based on the previous completed eligible 1-minute crypto bar low; missing,
forming, zero-trade, or synthetic bars do not advance it. The long trail is
monotonic and never moves downward.

Alpaca crypto supports simple `stop_limit` protection, but not native crypto
brackets, OCO, or trailing-stop orders. The gateway therefore owns the ladder
state and persists lots, rung attempts, fills, and stop generations in SQLite.
Because a resting protection order reserves the position quantity, stop changes
use this sequence:

```text
cancel old protection → place resized replacement → confirm replacement →
submit the TP tranche
```

The live `LotSupervisor` must receive each newly opened lot immediately. Startup
recovery also reloads open lots from SQLite, but a restart is not required for a
new alert to become active. This handoff is covered by production-path
regression tests; tests must not call `supervisor.adopt()` before exercising the
execution path under test.

For a safe paper-only arming session:

```bash
set -a; . ./.env; set +a
export PAPER_TRADING=true
export TRADING_ENABLED=true
export ALPACA_STREAM_ENABLED=true
export ALPACA_MARKET_DATA_FEED=iex
export MARKET_SYMBOLS=QQQ
export CRYPTO_SYMBOLS='BTC/USD,ETH/USD,ETH/BTC'
export GATEWAY_DB_PATH=/tmp/tv-master-paper.sqlite3

uv run uvicorn tv_alpaca_gateway.app:app \
  --host 127.0.0.1 --port 8000 --log-level info
```

In another shell, verify the process before sending an alert:

```bash
curl -s http://127.0.0.1:8000/healthz | python3 -m json.tool
```

Confirm `paper_trading` and `trading_enabled` are both `true`, the reported
commit is the intended checkout, and `market`, `crypto`, and `trade_updates`
are all `connected`. Stop with `Ctrl-C` when finished. Set
`TRADING_ENABLED=false` to keep the gateway running but disarmed. Never print or
commit `.env` or broker credentials.

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

## Test and deployment boundaries

The repository is paper-only by design. Keep disposable review checkouts,
clones, and test databases outside the canonical project directory; for this
environment use `/home/wzhao/sandbox`. Do not create project worktrees directly
under the user's home directory.

The current implementation remains a reviewed paper integration, not a live
trading system. Before any unattended use, add a durable background execution
queue, explicit operator controls, stronger restart reconciliation, monitoring,
and a live-environment design review. No live credentials or live Alpaca URL
are accepted by `Settings.validate()`.
