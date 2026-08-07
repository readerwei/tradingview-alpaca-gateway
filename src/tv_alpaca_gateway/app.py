from __future__ import annotations

import hmac
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

from .broker import AlpacaPaperClient
from .config import Settings
from .models import AlertError, Signal
from .notifier import DiscordNotifier, NullNotifier
from .risk import RiskError, approve
from .store import EventStore


def create_app(
    settings: Settings | None = None,
    broker: Any | None = None,
    store: EventStore | None = None,
    notifier: Any | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    store = store or EventStore(settings.db_path)
    broker = broker or AlpacaPaperClient(settings)
    notifier = notifier or (DiscordNotifier(settings.discord_webhook_url) if settings.discord_webhook_url else NullNotifier())
    app = FastAPI(title="TradingView Alpaca Gateway", version="0.1.0")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "paper_trading": settings.paper_trading, "trading_enabled": settings.trading_enabled}

    @app.post("/webhooks/tradingview")
    async def tradingview_webhook(request: Request, x_tv_secret: str | None = Header(default=None)) -> dict[str, Any]:
        raw = await request.body()
        if not settings.webhook_secret or not x_tv_secret or not hmac.compare_digest(x_tv_secret, settings.webhook_secret):
            raise HTTPException(status_code=401, detail="invalid webhook secret")
        try:
            payload = json.loads(raw)
            signal = Signal.parse(payload)
        except (json.JSONDecodeError, AlertError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not store.claim(signal.event_id):
            return {"accepted": False, "duplicate": True, "event_id": signal.event_id}
        try:
            order = approve(signal, settings)
        except RiskError as exc:
            store.update(signal.event_id, "rejected", str(exc))
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if not settings.trading_enabled:
            store.update(signal.event_id, "kill_switch", "TRADING_ENABLED=false")
            return {"accepted": True, "executed": False, "reason": "kill_switch", "event_id": signal.event_id}
        client_order_id = f"tv-{signal.event_id[:110]}"
        try:
            result = broker.submit(order, client_order_id)
            store.update(signal.event_id, "submitted", result.order_id)
            notifier.send(
                f"TradingView paper order: {order.side.upper()} {order.qty} {order.symbol} "
                f"limit ${order.limit_price:.2f}; status={result.status}; order_id={result.order_id}"
            )
            return {"accepted": True, "executed": True, "event_id": signal.event_id, "order_id": result.order_id, "status": result.status}
        except Exception as exc:
            store.update(signal.event_id, "failed", str(exc))
            notifier.send(f"TradingView paper order FAILED: {signal.symbol} {signal.action}: {exc}")
            raise HTTPException(status_code=502, detail="broker submission failed") from exc

    return app


app = create_app()
