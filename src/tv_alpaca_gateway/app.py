from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

from .broker import AlpacaPaperClient
from .config import Settings
from .models import AlertError, Signal
from .pine_alert_parser import AlertParseError as PineAlertParseError, PineOrderCommand, parse_pine_alert
from .notifier import DiscordNotifier, NullNotifier
from .risk import RiskError, approve
from .stream import AlpacaStreamManager, MarketQuote, MarketTrade, OrderUpdate
from .store import EventStore

logger = logging.getLogger(__name__)


def _pine_command_payload(command: PineOrderCommand) -> dict[str, Any]:
    """Return a JSON-safe command record; this never represents a broker order."""
    return {
        "symbol": command.symbol,
        "side": command.side,
        "qty": str(command.qty),
        "order_type": command.order_type,
        "time_in_force": command.time_in_force,
        "cancel_unfilled_at_deadline": command.cancel_unfilled_at_deadline,
        "place_protective_stop_after_fill": command.place_protective_stop_after_fill,
        "stop_trigger": str(command.stop_trigger) if command.stop_trigger is not None else None,
        "stop_limit": str(command.stop_limit) if command.stop_limit is not None else None,
        "trail": str(command.trail) if command.trail is not None else None,
    }


MAX_PINE_ALERT_BYTES = 4096

# Everything the dry-run deliberately does not evaluate. Kept beside the
# endpoint so it cannot drift from what the route actually skips.
_DRY_RUN_NOT_CHECKED = (
    "allowlist", "sizing", "notional", "price_collar", "kill_switch",
    "alert_freshness", "duplicate_event_id",
)


async def _read_limited_pine_body(request: Request) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_PINE_ALERT_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Pine alert exceeds {MAX_PINE_ALERT_BYTES} byte limit",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def create_app(
    settings: Settings | None = None,
    broker: Any | None = None,
    store: EventStore | None = None,
    notifier: Any | None = None,
    stream: AlpacaStreamManager | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    store = store or EventStore(settings.db_path)
    broker = broker or AlpacaPaperClient(settings)
    notifier = notifier or (DiscordNotifier(settings.discord_webhook_url) if settings.discord_webhook_url else NullNotifier())

    async def on_quote(event: MarketQuote) -> None:
        logger.debug("market quote %s bid=%s ask=%s", event.symbol, event.bid_price, event.ask_price)

    async def on_trade(event: MarketTrade) -> None:
        logger.debug("market trade %s price=%s size=%s", event.symbol, event.price, event.size)

    async def on_order_update(event: OrderUpdate) -> None:
        status = f"broker_{event.status or event.event}"
        detail = f"event={event.event}; filled_qty={event.filled_qty}"
        updated = store.update_by_order_id(event.order_id, status, detail)
        logger.info("Alpaca order update order_id=%s event=%s status=%s", event.order_id, event.event, event.status)
        notifier.send(
            f"Alpaca paper order update: {event.side.upper()} {event.qty} {event.symbol}; "
            f"event={event.event}; status={event.status}; filled={event.filled_qty}"
        )
        if not updated:
            logger.warning("received update for unknown order_id=%s", event.order_id)

    async def on_stream_error(error: Exception) -> None:
        logger.warning("Alpaca stream error: %s", error)

    async def resync_orders_after_reconnect() -> None:
        """Re-read every non-terminal order straight from the broker.

        Alpaca does not replay trade_updates that occurred while the socket was
        down, so a reconnect leaves a hole. Without this the store keeps an
        order's last pre-outage status forever — reporting a position as working
        when it actually filled, which is the expensive direction to be wrong in.

        Runs before the socket is read, so the gap closes even if no further
        update ever arrives.
        """
        order_ids = store.unresolved_broker_orders()
        if not order_ids:
            return
        logger.info("resyncing %d unresolved order(s) after stream connect",
                    len(order_ids))
        for order_id in order_ids:
            try:
                # get_order is blocking urllib; keep it off the event loop.
                result = await asyncio.to_thread(broker.get_order, order_id)
            except Exception:
                logger.exception("resync failed for order_id=%s", order_id)
                continue
            if store.update_by_order_id(order_id, f"broker_{result.status}",
                                        "resynced after stream reconnect"):
                logger.info("resynced order_id=%s status=%s", order_id, result.status)

    stream = stream or (
        AlpacaStreamManager(settings, on_quote, on_trade, on_order_update,
                            on_stream_error, resync_orders_after_reconnect)
        if settings.stream_enabled
        else None
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if stream is not None and settings.stream_enabled:
            await stream.start()
        try:
            yield
        finally:
            if stream is not None and settings.stream_enabled:
                await stream.stop()

    app = FastAPI(title="TradingView Alpaca Gateway", version="0.1.0", lifespan=lifespan)
    # Exposed so the streams can be inspected and exercised from outside —
    # an end-to-end test needs to trigger a resync without reaching into a
    # closure, and an operator needs a way to see whether a socket is up.
    app.state.stream = stream
    app.state.store = store
    app.state.broker = broker

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "paper_trading": settings.paper_trading, "trading_enabled": settings.trading_enabled}

    @app.post("/webhooks/tradingview/pine/dry-run")
    async def pine_dry_run(request: Request, x_tv_secret: str | None = Header(default=None)) -> dict[str, Any]:
        """Authenticate, parse, and audit a Pine command without reaching risk or broker code."""
        if not settings.webhook_secret or not x_tv_secret or not hmac.compare_digest(x_tv_secret, settings.webhook_secret):
            raise HTTPException(status_code=401, detail="invalid webhook secret")
        try:
            raw = (await _read_limited_pine_body(request)).decode("utf-8")
            command = parse_pine_alert(raw)
        except (UnicodeDecodeError, PineAlertParseError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        command_payload = _pine_command_payload(command)
        audit_id = "pine-dry-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
        store.record_pine_dry_run(audit_id, json.dumps(command_payload, sort_keys=True))
        # State the scope in the response. "dry_run": true reads as "this is
        # what would happen if I sent it", but nothing here consults the risk
        # layer: an alert for an unlisted symbol, with no configured size, over
        # the notional cap, with the kill switch on, still returns 200. Four
        # refusals, one green light. Naming what was NOT checked costs nothing
        # and stops a parse being mistaken for an approval.
        return {
            "dry_run": True,
            "validated": "parse_only",
            "not_checked": list(_DRY_RUN_NOT_CHECKED),
            "audit_id": audit_id,
            "command": command_payload,
        }

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
            price_lookup = getattr(broker, "latest_trade_price", None)
            market_price = price_lookup(signal.symbol) if price_lookup is not None else None
            order = approve(signal, settings, reference_price=market_price)
        except RiskError as exc:
            store.update(signal.event_id, "rejected", str(exc))
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except Exception as exc:
            store.update(signal.event_id, "market_data_failed", str(exc))
            raise HTTPException(status_code=503, detail="market data unavailable") from exc
        if not settings.trading_enabled:
            store.update(signal.event_id, "kill_switch", "TRADING_ENABLED=false")
            return {"accepted": True, "executed": False, "reason": "kill_switch", "event_id": signal.event_id}
        client_order_id = "tv-" + hashlib.sha256(signal.event_id.encode("utf-8")).hexdigest()
        try:
            result = broker.submit(order, client_order_id)
            store.update(signal.event_id, "submitted", result.order_id, broker_order_id=result.order_id)
        except Exception as exc:
            store.update(signal.event_id, "failed", str(exc))
            store.release(signal.event_id)
            raise HTTPException(status_code=502, detail="broker submission failed") from exc
        try:
            notifier.send(
                f"TradingView paper order: {order.side.upper()} {order.qty} {order.symbol} "
                f"limit ${order.limit_price:.2f}; status={result.status}; order_id={result.order_id}"
            )
        except Exception:
            logger.exception("paper-order receipt notification failed for order_id=%s", result.order_id)
        return {"accepted": True, "executed": True, "event_id": signal.event_id, "order_id": result.order_id, "status": result.status}

    return app


app = create_app()
