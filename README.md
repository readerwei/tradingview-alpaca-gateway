# TradingView → Alpaca Gateway

A small, deterministic webhook receiver for TradingView signals. It validates alerts, enforces an allowlist and risk limits, deduplicates event IDs, submits **paper-only** Alpaca orders, and optionally posts receipts to Discord.

## Safety defaults

- `PAPER_TRADING=true` is required.
- `TRADING_ENABLED=false` is the default kill switch.
- Live Alpaca URLs are rejected.
- Position quantity is determined server-side; TradingView cannot override it.
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

## Tests

```bash
uv run --extra test pytest
```

## Architecture

```text
TradingView → HTTPS endpoint → authentication → schema/freshness checks
             → allowlist/risk gate → SQLite idempotency → Alpaca paper API
             → optional Discord receipt
```

For production, put the service behind a managed HTTPS reverse proxy, add a durable queue/worker, rotate secrets, monitor failures, and keep the kill switch accessible outside the request process.
