# Repository Guidelines

## Project Structure & Component Boundaries

The core application is in `src/tv_alpaca_gateway/`. `app.py` owns the FastAPI
application; `main.py` exposes the `tv-alpaca-gateway` command; execution, broker,
risk, persistence, and stream concerns are split into focused modules such as
`execution.py`, `broker.py`, `risk.py`, `store.py`, and `stream.py`.

The optional Discord relay currently lives in the same repository but is a
separate package under `src/tv_alpaca_relay/`. It is a separate process and
component, not part of the gateway runtime. The relay owns Discord connectivity,
source-channel/webhook admission, and forwarding to the gateway. The gateway
must not depend on Discord at runtime.

The intended long-term direction is to move the relay into a separate repository,
but keep one repository for now while the HTTP contract and cross-component
integration tests mature. Do not mix relay implementation into gateway modules.

Tests live in `tests/` and mirror feature or integration boundaries. The root
`integration_relay.py` is a local relay-to-gateway dry-run harness. Runtime
configuration is environment-driven; `.env.example` documents expected variables.
Never commit `.env`, broker credentials, Discord tokens, webhook secrets, or live
trading configuration.

## Build, Test, and Run Commands

Use Python 3.11+ and `uv`:

```bash
uv sync --extra test                 # application and test dependencies
uv run pytest                        # complete test suite
uv run tv-alpaca-gateway             # intended gateway startup command
```

For local paper testing, copy `.env.example` to `.env`, load it with:

```bash
set -a; . ./.env; set +a
export PAPER_TRADING=true
export TRADING_ENABLED=true
```

Do not use `uv run uvicorn tv_alpaca_gateway.app:app` as the normal startup
command. Direct Uvicorn bypasses `main.py` and therefore bypasses the project's
logging initialization. Use `uv run tv-alpaca-gateway`.

### Optional relay process

The relay is started separately, in a second terminal:

```bash
uv sync --extra relay
set -a; . ./.env; set +a
uv run tv-alpaca-relay
```

The relay requires `DISCORD_BOT_TOKEN`, `DISCORD_SIGNAL_CHANNEL_ID`,
`DISCORD_SOURCE_WEBHOOK_ID`, and `TV_WEBHOOK_SECRET`. It defaults to the safe
parse-only target:

```text
GATEWAY_INTERNAL_URL=http://127.0.0.1:8000/webhooks/tradingview/pine/dry-run
```

Paper execution through the relay requires explicit configuration of both:

```text
GATEWAY_INTERNAL_URL=http://127.0.0.1:8000/webhooks/tradingview/pine/submit
RELAY_ALLOW_EXECUTION=true
```

Do not enable relay execution merely to test connectivity. Verify the dry-run
boundary first. The relay process and gateway process have separate stdout and
lifecycle; inspect both when diagnosing delivery.

## HTTP and Identity Contracts

The Pine route accepts raw pipe-delimited text, not the generic JSON format:

```text
EXECUTE_ALPACA_ORDER | SYMBOL=QQQ | SIDE=BUY | QTY=1 | ORDER_TYPE=MARKET | TIME_IN_FORCE=DAY
```

The relevant routes are:

```text
POST /webhooks/tradingview/pine/dry-run
POST /webhooks/tradingview/pine/submit
```

Requests require `Content-Type: text/plain` and `X-TV-Secret`. The relay must
forward the raw alert unchanged and include a canonical delivery identity. The
current implementation uses the relay's Discord message identity as the
`delivery_id`; the planned migration is to introduce generic `X-Delivery-ID` (or
`X-Idempotency-Key`), temporarily accept `X-Discord-Message-Id` as a compatibility
alias, test duplicate submissions through both headers, then remove the
Discord-specific alias.

Preserve idempotency semantics during this migration. A retry or duplicate alert
must not submit a second entry. An alert with neither a valid `EVENT_ID` nor a
valid delivery identity must not reach the broker.

## Safety and Verification Requirements

- Keep `PAPER_TRADING=true` for local and CI work.
- Treat broker state as external until verified by broker IDs, fills, and positions.
- Do not claim a gateway or relay is running without checking the live process,
  listener, and `/healthz` response in the same environment.
- A successful local `/healthz` does not prove TradingView can reach the machine
  or that the relay is connected.
- Verify the complete delivery boundary with an explicit dry-run before paper
  execution.
- Preserve relay admission tests for approved webhook/channel, wrong channel,
  wrong webhook, human-authored messages, malformed messages, and canonical
  Discord/delivery identity.
- Preserve gateway tests for authentication, parser behavior, risk refusal,
  duplicate/idempotency behavior, fill handling, protection, and restart recovery.
- Keep a cross-component integration test proving an approved relay message is
  forwarded to the gateway while unapproved/human messages are rejected.
- Run the full test suite after changes and inspect `git status`, exact commit,
  remote head, and changed paths before reporting delivery.

## Coding and Documentation Conventions

Use four-space indentation, type annotations where they clarify boundaries,
`snake_case` for modules/functions/variables, `PascalCase` for classes, and
`UPPER_SNAKE_CASE` for environment variables. Prefer small explicit functions and
avoid unrelated rewrites. No formatter or linter is currently configured; match
nearby code.

Document gateway and relay startup as separate processes. Keep the README's
quick-start section and detailed architecture section consistent. Do not describe
the relay as automatically started by the gateway.

## Git and Delivery Guidelines

Use concise imperative commit subjects, optionally with conventional prefixes.
Keep commits focused. Before committing, run `git diff --check` and the relevant
tests. Before pushing, confirm the intended files are staged and pre-existing
untracked files are excluded unless explicitly requested.

The staged architecture plan is:

1. Keep gateway and relay in one repository but separate packages/processes.
2. Establish the generic delivery/idempotency HTTP contract and compatibility
   migration.
3. Add cross-component integration coverage.
4. Move the relay to a separate repository only after those contracts and tests
   have a verified home.
