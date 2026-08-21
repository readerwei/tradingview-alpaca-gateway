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
uv run tv-alpaca-gateway
```

Health check:

```bash
curl http://127.0.0.1:8000/healthz
```

## Optional Discord relay

The Discord relay is a **separate process** from the gateway. Start the gateway
first, then start the relay in a second terminal. TradingView sends to the
Discord incoming webhook; the relay observes the approved Discord message and
forwards the raw Pine alert to the local gateway.

### Terminal 1 — gateway

```bash
set -a; . ./.env; set +a
export PAPER_TRADING=true
export TRADING_ENABLED=true
uv run tv-alpaca-gateway
```

### Terminal 2 — relay

Install the optional Discord dependency once per virtual environment:

```bash
uv sync --extra relay
```

Then start the relay:

```bash
set -a; . ./.env; set +a
uv run tv-alpaca-relay
```

The relay requires `DISCORD_BOT_TOKEN`, `DISCORD_SIGNAL_CHANNEL_ID`,
`DISCORD_SOURCE_WEBHOOK_ID`, and `TV_WEBHOOK_SECRET`. It defaults to the safe
parse-only route:

```text
http://127.0.0.1:8000/webhooks/tradingview/pine/dry-run
```

Keep the two processes running in separate terminals. Submit forwarding returns
`202 Accepted` with the entry order ID once the gateway has validated, claimed,
and submitted the entry. Fill tracking and protective-order management continue
in the gateway background; a relay timeout must not be retried blindly. The
relay sends the generic `X-Delivery-ID` header (the gateway temporarily accepts
`X-Discord-Message-Id` for compatibility).

See [Relay deployment and
verification](#start-the-relay-separately) below for the execution opt-in,
logging, and end-to-end verification steps.

## Direct parsed-alert runner

For paper testing, the same parsed Pine command can be run without TradingView,
Discord, or webhook HTTP. The runner uses the real parser, risk checks,
execution engine, broker adapter, SQLite store, supervisor, and streams.
Without `--execute` it is parse-only; `--execute` is required to submit an
order and still requires `PAPER_TRADING=true`. It stays alive after setup so
managed exits continue to receive stream events; use `--once` for a
broker-held native equity OCO test.

```bash
# Parse only (safe default)
cat alert.txt | uv run tv-alpaca-run-alert

# Execute and keep managing the paper position.
# NOTE: --once returns as soon as the entry fills, which closes the lifespan
# and takes the supervisor, the sockets and the reconcile timer with it. An
# alert carrying an EXIT_PLAN is refused with --once for that reason: it would
# arm the disaster stop, write the lot, and leave nothing listening. That
# combination cost two days of runs that armed correctly and never fired.
cat alert.txt | uv run tv-alpaca-run-alert --execute

# Execute a native equity OCO and exit after the broker accepts it
uv run tv-alpaca-run-alert --alert-file qqq-oco.txt --execute --once
```

Do not run this against a live environment. Before `--execute`, verify
`/healthz`, `PAPER_TRADING=true`, the merged commit, the account position, and
open orders. Do not start a new managed lot while another lot for that symbol
is open.

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

The clients authenticate, subscribe, reconnect with bounded exponential backoff, and stop cleanly with the application. Order updates are matched to submitted orders by broker order ID and update the SQLite event record; partial-fill and terminal-status details are also sent to the optional Discord notifier. Notification failures are isolated and logged; they cannot tear down the Alpaca stream or prevent the fill from reaching the supervisor. The default remains disabled, so normal tests and local webhook use do not open network connections.

This stream is still paper-only and is not a live-trading safety certification. Before any unattended use, add a durable outbox/retry state machine, restart reconciliation, position-aware sell checks, and persisted managed-exit state.

## Pine command parser (current TradingView alert format)

The repository includes `parse_pine_alert()` for the current pipe-delimited Pine command format:

```text
EXECUTE_ALPACA_ORDER | SYMBOL=BTCUSD | SIDE=BUY | QTY=0.001 | ORDER_TYPE=MARKET | TIME_IN_FORCE=GTC | CANCEL_UNFILLED_AT_DEADLINE=YES | PLACE_PROTECTIVE_STOP_AFTER_FILL | STOP_TRIGGER=65000 | STOP_LIMIT=64950 | TRAIL=NONE
```

It parses and validates `SYMBOL`, `SIDE`, `QTY`, `ORDER_TYPE`, `TIME_IN_FORCE`, `CANCEL_UNFILLED_AT_DEADLINE`, the protective-stop flag, `STOP_TRIGGER`, `STOP_LIMIT`, `TAKE_PROFIT`, and `TRAIL`. The current parser accepts only `ORDER_TYPE=MARKET`; it will not represent a non-market entry until the contract gains explicit entry-price fields. `BTCUSD` is normalized to `BTC/USD`; `TRAIL=NONE` becomes no trail, while `TRAIL=250` means a $250 trail distance. The parser deliberately ignores non-executable instruction fields such as `REQUIRED_ACTIONS` and `DO_NOT_SUMMARIZE_OR_REPOST_BEFORE_BROKER_CALL`.

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

### Native equity OCO after fill

For equities, `EXIT_PLAN=OCO_AFTER_FILL` arms one native Alpaca OCO exit after
the entry fills. The exit is sized from the measured position delta (not the
requested entry quantity), uses a sell limit take-profit plus a stop-market
stop-loss, and is submitted with client ID `<event-id>-oco`:

```text
EXECUTE_ALPACA_ORDER | SYMBOL=QQQ | SIDE=BUY | QTY=302 | ORDER_TYPE=MARKET | TIME_IN_FORCE=GTC | EVENT_ID=qqq-oco-1 | EXIT_PLAN=OCO_AFTER_FILL | STOP_TRIGGER=723.65 | STOP_LIMIT=NONE | TAKE_PROFIT=724.89
```

`TAKE_PROFIT` and `STOP_TRIGGER` are required. `STOP_LIMIT=NONE` means a
stop-market leg; a numeric `STOP_LIMIT` creates a stop-limit leg. This plan
does not use `INTERVAL` and is rejected for crypto. `DYNAMIC_TRAIL` remains
unchanged and still requires `INTERVAL`.

### Long and short managed ladders

`DYNAMIC_TRAIL` uses one signed-direction implementation for both equity
positions:

- `SIDE=BUY` opens a long: targets are above entry, exits are sells, and the
  disaster stop is below entry.
- `SIDE=SELL` opens a short: targets are below entry, exits are buys, and the
  disaster stop is above entry.

The short entry is preflighted before the broker order is submitted. The asset
must be confirmed `shortable=true`; an asset lookup failure is refused rather
than treated as permission. Alpaca crypto spot assets report `shortable=false`
and cannot be sold short.

Alpaca also marks fractional sell orders as long rather than opening a short.
Therefore a fractional `SIDE=SELL` with an `EXIT_PLAN` is rejected before
submission. A fractional sell **without** an exit plan remains allowed because
that is the normal way to close a fractional long. Whole-share equity shorts
with a managed plan are supported.

Examples:

```text
# Managed long
SIDE=BUY | QTY=10 | EXIT_PLAN=DYNAMIC_TRAIL | INTERVAL=1m |
STOP_TRIGGER=<below-entry> | STOP_LIMIT=<below-trigger>

# Managed whole-share short
SIDE=SELL | QTY=10 | EXIT_PLAN=DYNAMIC_TRAIL | INTERVAL=1m |
STOP_TRIGGER=<above-entry> | STOP_LIMIT=<above-trigger>

# Rejected: fractional sell cannot open a short
SIDE=SELL | QTY=10.5 | EXIT_PLAN=DYNAMIC_TRAIL | INTERVAL=1m
```

The named `DYNAMIC_TRAIL` plan is paper-only and must be requested explicitly in
the Pine alert:

The rules in one place, so the algebra can be checked rather than trusted:

```text
R              sign * (entry - stop)          positive both ways
target(n)      entry + sign * multiple * R    above entry long, below it short
reached        sign * (price - level) >= 0
breached       sign * (price - stop)  <= 0
trail          long ratchets UP on bar LOWS
               short ratchets DOWN on bar HIGHS
exit side      long sells, short buys
stop order     long: sell below entry, limit under the trigger
               short: buy above entry, limit over it
```

The same code runs both ways, so a mistake appears in both directions rather
than hiding in the one nobody exercised. The contract tests run every rule
twice for the same reason.

### The plans

| plan | tranches | use |
|---|---|---|
| `DYNAMIC_TRAIL` | 20% @ 1.2R, 30% @ 2.5R, 50% runner | the strategy |
| `DYNAMIC_TRAIL_FAST` | same splits at 0.2R / 0.4R | testing — targets reachable in minutes |
| `OCO_AFTER_FILL` | one target, whole position | take-profit and stop, whichever comes first |
| `SMART_PROFIT` | 30% / 40% / 30%, armed by structure | let the trend decide the targets |

`DYNAMIC_TRAIL_FAST` exists so that testing is an alert field rather than an
edit to `exit_plans.py`. Editing the real plan's multiples in place left the
repository saying 1.2R/2.5R while the gateway ran 0.2R/0.4R — a divergence
`/healthz` could not report, because an uncommitted edit carries the same commit
hash as the code it changes. `/healthz` now also reports `worktree_dirty`.

### SMART_PROFIT — slices armed by market structure

The other plans fire a rung when price **reaches** a level chosen in advance.
`SMART_PROFIT` sells a slice when price **falls back through** a level the market
chose, so the targets are discovered rather than predicted.

```text
arming    a bar whose low beats EVERY low since counting began -> count++
          N = 3 of those, and above entry + 0.5R                -> arm the slice
armed     each new higher low raises that slice's trail
sold      price breaks the trail -> sell the slice, stop to breakeven,
                                    counting restarts from zero for the next
weakening M = 2 CONSECUTIVE lower lows -> every remaining share collapses onto
                                          one 0.1R trail hung off the run's peak
```

Slices are 30% / 40% / 30%, and only one is ever armed at a time — counting
belongs to whichever slice is next, so a slice cannot accumulate structure while
its predecessor is still running.

Two rules are worth stating because they are asymmetric on purpose:

- **A lower low does not reset the arming count**, it merely fails to increment
  it. A dip inside a climb costs a bar, not the sequence.
- **Weakness does require consecutive lower lows.** One wide bar is noise; two
  in a row is the structure failing.

The `entry + 0.5R` gate exists because three higher lows can happen entirely
below entry — arming there would sell at a loss under the name take-profit. The
weak trail never widens either: a 0.1R trail hung off a high water mark barely
above entry can sit further from price than the disaster stop, and adopting it
would answer weakness with more risk.

This plan consumes **bars**, not prints, and ignores zero-trade bars. On
Alpaca's crypto feed, where only 34% of 1-minute bars contain a trade, `N=3` can
mean twenty minutes of wall clock; on TSLA it is three minutes. Pick `N` per
symbol accordingly.

The named `DYNAMIC_TRAIL` plan must be requested explicitly in the Pine alert:
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
based on the previous completed eligible 1-minute bar: the long trail ratchets
on bar lows, while the short trail ratchets on bar highs. Missing, forming,
zero-trade, or synthetic bars do not advance it. Each trail is monotonic in its
profitable direction and never moves back toward greater risk.

Quotes never fire a rung: they are counted and logged, and never reach the
supervisor.

The bar path exists because of the feed, not for elegance. Measured over twelve
hours of Alpaca's BTC/USD 1-minute bars, 479 of 719 minutes produced a bar at
all and only 167 of those contained a trade. A target can be crossed and
abandoned between prints, so a rung would get one chance and sometimes none.

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

## Observability

The gateway spent a week being unable to say why nothing was happening. Six live
runs armed correctly and did nothing, and no log said how far the target was or
why a bar had been skipped. What was missing was never the data — it was the
reasoning.

```bash
LOG_LEVEL=DEBUG        # decisions: why a rung did not fire, why a bar was skipped
LOG_MARKET_DATA=true   # the per-message firehose, separately and rarely
HEARTBEAT_SECONDS=60   # lot state on a timer even when nothing changes; 0 disables
```

`LOG_LEVEL=DEBUG` gives roughly three lines a minute plus whatever actually
happened:

```text
INFO  market BTC/USD: 412 trades, 30 bars (15 traded), 0 quotes
INFO  market QQQ: 0 trades, 0 bars (0 traded), 0 quotes
INFO  heartbeat: lot demo BTC/USD stage=ladder remaining=0.0015 working_stop=63800
      reserved=0.0015 filled=[] pending=tp1@64240.0 last_price=63900.0
```

Two of those lines earn their place specifically:

- **`QQQ: 0 trades, 0 bars`** — a stream that is connected and delivering
  nothing looks exactly like a quiet market. Telling them apart took two days
  the first time.
- **the heartbeat** — a process with nothing to do and one that has silently
  stopped are identical in an event-driven log, by construction: no events, no
  lines.

Per-message logging lives on `tv_alpaca_gateway.marketdata`, muted explicitly
rather than by omission — it is a child of the package logger and would
otherwise inherit `DEBUG` and drown everything else.

`/healthz` reports the running commit, whether the worktree is dirty, and the
connection state of each stream:

```json
{"ok": false, "commit": "cfdae66…", "worktree_dirty": false,
 "streams": {"market": "connected", "crypto": "connected",
             "trade_updates": "down: InvalidStatus: HTTP 403"}}
```

`ok` means the sockets are connected, not merely that the process answers HTTP.

Note the distinction, because conflating the two is how a green check reassures
you about something it never examined: **`/healthz` reports whether a socket is
connected. The periodic market summary reports whether data is arriving.** A
stream can be connected and silent, which is exactly the state that took two
days to recognise — `market QQQ: 0 trades, 0 bars` is the line that shows it,
and it comes from the heartbeat, not from `/healthz`.

## One connection per account

Alpaca allows **one market-data connection per feed and one trade-update
connection, per account**. Two processes on the same credentials contend: each
connect evicts the other.

If another system of yours streams the same account, the gateway cannot get the
feed at all, and its subscription line simply never appears. A second paper
account is the fix; more API keys on the same account are not, because the limit
is per account.

A related failure is worth recording because it cost a day and looked exactly
like contention. Every order update was followed ~150ms later by:

```text
WARNING Alpaca trade_updates stream disconnected: HTTP Error 403: Forbidden
```

That was not eviction. `on_order_update` called the Discord notifier, Discord
returned 403, `urllib.error.HTTPError` is a subclass of `OSError`, and the
reconnect handler caught it as a stream failure — tearing down a healthy socket
on every order update. Worse, the notification came *before* the line routing
the fill to the lot, so the exception skipped it: the primary fill path was dead
and only the reconcile timer kept the ladder correct.

The error text was the tell. `HTTP Error 403: Forbidden` is a urllib error; a
websocket eviction raises `ConnectionClosed` with a close code. Notifications
now never raise at their caller, fills are routed before notifying, and handler
exceptions can no longer masquerade as disconnects.

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

For a deployment with no public broker webhook, use the optional Discord relay.
The relay is a separate process and a separate component from the gateway:

```text
TradingView → Discord incoming webhook → private signal channel
             → outbound Python Discord relay → 127.0.0.1 gateway → Alpaca paper API
```

The gateway process owns the FastAPI routes, Alpaca streams, risk checks, execution,
SQLite state, and managed exits. The relay process owns Discord connectivity and
the external admission boundary. It admits only messages from the configured
channel and source webhook ID, requires Discord's Message Content intent, forwards
the raw pipe-delimited Pine command unchanged, and ignores human messages, other
bots, other channels, malformed content, and unapproved webhooks.

TradingView does not call the local gateway directly. It calls the Discord
incoming webhook. Discord then creates a message, and the relay bot observes that
message and forwards it to the gateway with `X-TV-Secret` and
`X-Discord-Message-Id` headers. Keep the bot token, source webhook URL, and broker
credentials out of Git and out of Discord messages.

### Start the gateway

```bash
set -a; . ./.env; set +a
export PAPER_TRADING=true
export TRADING_ENABLED=true
uv run tv-alpaca-gateway
```

Keep this process running. It listens on `127.0.0.1:8000` and exposes the
gateway routes. The project command must be used rather than invoking Uvicorn
directly because it loads the gateway settings and configures application
logging before starting Uvicorn.

### Start the relay separately

Install the optional Discord dependency once per virtual environment:

```bash
uv sync --extra relay
```

Then, in a second terminal, load the same environment and start the relay:

```bash
set -a; . ./.env; set +a
uv run tv-alpaca-relay
```

The relay requires these variables:

```text
DISCORD_BOT_TOKEN
DISCORD_SIGNAL_CHANNEL_ID
DISCORD_SOURCE_WEBHOOK_ID
TV_WEBHOOK_SECRET
```

`GATEWAY_INTERNAL_URL` defaults to the safe parse-only route:

```text
http://127.0.0.1:8000/webhooks/tradingview/pine/dry-run
```

For an explicitly enabled paper execution relay, set both of these values after
the dry-run path has been verified:

```text
GATEWAY_INTERNAL_URL=http://127.0.0.1:8000/webhooks/tradingview/pine/submit
RELAY_ALLOW_EXECUTION=true
```

The relay prints a connection message when Discord login succeeds. Its stdout
contains admission and forwarding failures; the gateway's stdout contains the
HTTP request, parser, risk, broker, and protection results. Run the two processes
in separate terminals or redirect each process to a separate log file.

### Verify the boundary safely

Start with `GATEWAY_INTERNAL_URL` pointing to `pine/dry-run`. Send a current
pipe-delimited alert through the configured Discord incoming webhook and verify:

1. TradingView reports a successful Discord webhook request.
2. The relay prints that it connected and does not reject the message.
3. The gateway records a `200` dry-run response.

Only after those three checks should a paper execution target be enabled. The
relay does not retry forwarding failures, and the gateway's `EVENT_ID` or the
relay's Discord message ID provides the duplicate identity used by execution.

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
