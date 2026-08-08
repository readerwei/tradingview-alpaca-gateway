from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from . import assets
from .broker import AlpacaPaperClient, FakeBroker
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
    # Validate before constructing or accepting injected dependencies.  The
    # default Alpaca client validates itself, but dependency injection must not
    # create a bypass around the paper-only configuration invariant.
    settings.validate()
    store = store or EventStore(settings.db_path)
    broker = broker or AlpacaPaperClient(settings)
    if type(broker) not in {AlpacaPaperClient, FakeBroker}:
        raise ValueError("Injected broker is not a trusted paper-only broker")
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

    def require_paper_execution() -> None:
        """Re-check the paper invariant at every executable request boundary.

        Startup validation protects normal construction, but an injected app
        can outlive a configuration mutation in a long-running process.  Do
        not let that turn a previously safe dependency injection into an
        executable bypass.
        """
        try:
            settings.validate()
        except ValueError as exc:
            raise HTTPException(status_code=503, detail="paper-only execution invariant is not satisfied") from exc
        if type(broker) not in {AlpacaPaperClient, FakeBroker}:
            raise HTTPException(status_code=503, detail="paper-only execution invariant is not satisfied")

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
        return {"dry_run": True, "audit_id": audit_id, "command": command_payload}

    @app.post("/webhooks/tradingview/pine/preview")
    async def pine_preview(request: Request, x_tv_secret: str | None = Header(default=None)) -> dict[str, Any]:
        """Run parser and server risk checks without claiming or submitting an order."""
        if not settings.webhook_secret or not x_tv_secret or not hmac.compare_digest(x_tv_secret, settings.webhook_secret):
            raise HTTPException(status_code=401, detail="invalid webhook secret")
        try:
            command = parse_pine_alert((await _read_limited_pine_body(request)).decode("utf-8"))
            if command.cancel_unfilled_at_deadline or command.place_protective_stop_after_fill or command.trail is not None:
                raise PineAlertParseError(
                    "lifecycle and protection controls are deferred until Phase 4"
                )
            if assets.resolve(command.symbol, settings.allowed_symbols) not in settings.allowed_symbols:
                raise RiskError("symbol is not allowlisted")
            reference_price = broker.latest_trade_price(command.symbol)
            received_at = datetime.now(timezone.utc)
            signal = Signal("pine-preview", command.symbol, command.side, "1m", received_at, reference_price)
            approve(signal, settings, reference_price=reference_price, requested_qty=command.qty)
            maximum = settings.crypto_max_qty if "/" in command.symbol else Decimal(settings.max_qty)
            if command.qty > maximum or command.qty * Decimal(str(reference_price)) > Decimal(str(settings.max_notional)):
                raise RiskError("requested quantity exceeds configured limit")
        except HTTPException:
            raise
        except (UnicodeDecodeError, PineAlertParseError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RiskError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail="market data unavailable") from exc
        return {"preview": True, "approved": True, "symbol": command.symbol, "side": command.side,
                "qty": str(command.qty), "order_type": "market", "timeframe": "1m"}

    @app.post("/webhooks/tradingview/pine/paper-submit")
    async def pine_paper_submit(request: Request, x_tv_secret: str | None = Header(default=None)) -> dict[str, Any]:
        """Submit one receipt-time, paper-only Pine entry with fail-closed retries.

        TradingView's alert can be replayed after receipt, so this route derives
        its durable event key from the raw body and never releases a claimed
        event after a submission exception: the broker may have accepted it.
        Lifecycle/protection fields remain parser-visible but are rejected until
        Phase 4 can persist and reconcile their state.
        """
        if not settings.webhook_secret or not x_tv_secret or not hmac.compare_digest(x_tv_secret, settings.webhook_secret):
            raise HTTPException(status_code=401, detail="invalid webhook secret")
        require_paper_execution()
        if not settings.trading_enabled:
            raise HTTPException(status_code=503, detail="paper submission is disabled")
        try:
            raw = (await _read_limited_pine_body(request)).decode("utf-8")
            command = parse_pine_alert(raw)
            if command.cancel_unfilled_at_deadline or command.place_protective_stop_after_fill or command.trail is not None:
                raise PineAlertParseError(
                    "lifecycle and protection controls are deferred until Phase 4"
                )
            if assets.resolve(command.symbol, settings.allowed_symbols) not in settings.allowed_symbols:
                raise RiskError("symbol is not allowlisted")
            reference_price = broker.latest_trade_price(command.symbol)
            maximum = settings.crypto_max_qty if "/" in command.symbol else Decimal(settings.max_qty)
            if command.qty > maximum:
                raise RiskError("requested quantity exceeds configured limit")
            if command.qty * Decimal(str(reference_price)) > Decimal(str(settings.max_notional)):
                raise RiskError("requested notional exceeds configured limit")
            event_id = "pine-submit-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
            signal = Signal(event_id, command.symbol, command.side, "receipt", datetime.now(timezone.utc), reference_price)
            # Pine's requested size is a gate, not authority.  The approved
            # order deliberately uses the server-configured quantity so an
            # alert cannot choose a larger or otherwise policy-inconsistent
            # executable size through dependency injection or parser changes.
            order = approve(signal, settings, reference_price=reference_price)
            order = replace(order, time_in_force=command.time_in_force)
        except HTTPException:
            raise
        except (UnicodeDecodeError, PineAlertParseError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RiskError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail="market data unavailable") from exc

        if not store.claim(event_id):
            return {"accepted": False, "duplicate": True, "event_id": event_id}
        client_order_id = "tv-" + hashlib.sha256(event_id.encode("utf-8")).hexdigest()
        try:
            result = broker.submit(order, client_order_id)
        except Exception as exc:
            # Submission has an unknown outcome. Retain the claim and do not
            # retry automatically: a timeout can follow a successful broker
            # acceptance, and replaying would double the paper position.
            store.update(event_id, "submission_ambiguous", str(exc))
            return JSONResponse(
                status_code=503,
                content={
                    "accepted": False,
                    "ambiguous": True,
                    "event_id": event_id,
                    "detail": "paper submission outcome is ambiguous; inspect broker state",
                },
            )
        if not isinstance(result.order_id, str) or not result.order_id.strip():
            # A successful HTTP response without a broker identifier cannot be
            # reconciled or correlated with trade updates. Treat it as
            # ambiguous rather than claiming a durable accepted submission.
            detail = "paper broker response did not contain an order id"
            try:
                store.update(event_id, "submission_ambiguous", detail)
            except Exception:
                logger.exception("could not persist missing-order-id ambiguity for event_id=%s", event_id)
            return JSONResponse(
                status_code=503,
                content={
                    "accepted": False,
                    "ambiguous": True,
                    "event_id": event_id,
                    "status": result.status,
                    "detail": detail,
                },
            )
        try:
            store.update(
                event_id,
                f"broker_{result.status}",
                json.dumps(result.raw, sort_keys=True, default=str),
                broker_order_id=result.order_id,
            )
        except Exception as exc:
            # The broker accepted the order, but the local receipt write failed.
            # Never turn that into a normal 5xx or release the claim: a caller
            # may replay the alert and create a duplicate. Make one best-effort
            # durable ambiguity update, retaining the broker id when SQLite is
            # available again; otherwise the existing claimed row still blocks
            # automatic retry and logs carry the broker result.
            logger.exception("paper submission receipt persistence failed for event_id=%s", event_id)
            try:
                store.update(
                    event_id,
                    "submission_ambiguous",
                    f"broker accepted order_id={result.order_id}; local receipt persistence failed: {exc}",
                    broker_order_id=result.order_id,
                )
            except Exception:
                logger.exception("could not persist ambiguous paper submission for event_id=%s", event_id)
            return JSONResponse(
                status_code=503,
                content={
                    "accepted": False,
                    "ambiguous": True,
                    "event_id": event_id,
                    "order_id": result.order_id,
                    "status": result.status,
                    "detail": "paper broker accepted the order but receipt persistence failed; inspect broker state",
                },
            )
        return {
            "accepted": True,
            "executed": True,
            "paper_only": True,
            "receipt_time_submission": True,
            "event_id": event_id,
            "order_id": result.order_id,
            "status": result.status,
        }

    @app.post("/webhooks/tradingview")
    async def tradingview_webhook(request: Request, x_tv_secret: str | None = Header(default=None)) -> dict[str, Any]:
        if not settings.webhook_secret or not x_tv_secret or not hmac.compare_digest(x_tv_secret, settings.webhook_secret):
            raise HTTPException(status_code=401, detail="invalid webhook secret")
        require_paper_execution()
        raw = await _read_limited_pine_body(request)
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
