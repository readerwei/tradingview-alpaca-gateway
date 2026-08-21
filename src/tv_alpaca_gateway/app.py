from __future__ import annotations

import asyncio
import contextlib
import hashlib
import pathlib
import hmac
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .broker import AlpacaPaperClient
from .config import Settings
from .models import AlertError, Signal
# Imported as a module, not as a bound name. The route's only job is to
# delegate, and calling through `execution.` keeps that seam visible and
# substitutable — a directly-bound function cannot be observed or replaced,
# which makes "does this actually delegate?" untestable.
from . import execution
from .execution import ExecutionError, UnprotectedPositionError
from .pine_alert_parser import AlertParseError as PineAlertParseError, PineOrderCommand, parse_pine_alert
from .notifier import DiscordNotifier, NullNotifier
from .risk import RiskError, approve
from .exit_manager import is_ours
from .lot_supervisor import LotSupervisor
from .market_log import MarketDataCounters
from .market_log import logger as market_logger
from .stream import (AlpacaStreamManager, MarketBar, MarketQuote, MarketTrade,
                     OrderUpdate)
from .store import EventStore

logger = logging.getLogger(__name__)

# How many distinct foreign order ids to remember before starting over. Only a
# leak guard: forgetting costs one repeated warning, which is the cheap side.
FOREIGN_ORDER_MEMORY = 2048


def _running_commit() -> str:
    """The commit this process is actually running, not the one on master.

    Three times in one day the runtime diverged from the repository — an
    unpushed commit, a stale checkout, and a process still running yesterday's
    code. The third produced a filled, unprotected position: master was correct
    and the process was not, and nothing exposed the difference.

    /healthz reported configuration, which is why "is it current?" had to be
    inferred from process start time against commit time. This makes it
    directly checkable instead.

    Resolved once at import. Returns "unknown" rather than raising if the
    process is not running from a git checkout — a deployment that cannot
    report its commit should still start, it just cannot prove its version.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=pathlib.Path(__file__).resolve().parent,
            capture_output=True, text=True, timeout=5, check=True)
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _worktree_dirty() -> bool | None:
    """Whether the running checkout has uncommitted edits.

    The commit hash caught three of four runtime/repository divergences. It
    could not catch the fourth: on 2026-08-14 the gateway ran DYNAMIC_TRAIL at
    0.2R/0.4R while master said 1.2R/2.5R, because exit_plans.py had been
    edited in place and never committed. An uncommitted edit has exactly the
    same commit hash as the code it changed, so "commit" answered the question
    correctly and the answer was still wrong.

    None rather than False when it cannot be determined, so "not a git
    checkout" is never reported as "clean".
    """
    import subprocess
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=pathlib.Path(__file__).resolve().parent,
            capture_output=True, text=True, timeout=5, check=True)
        return bool(result.stdout.strip())
    except Exception:
        return None


RUNNING_COMMIT = _running_commit()
WORKTREE_DIRTY = _worktree_dirty()


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
        counters.record_quote(event.symbol)
        market_logger.debug("market quote %s bid=%s ask=%s", event.symbol, event.bid_price, event.ask_price)

    supervisor = LotSupervisor(store, broker)
    counters = MarketDataCounters()
    # Order ids already reported as foreign, so one order is one line rather
    # than one per event. Bounded because this process runs for days against an
    # account another system also trades.
    foreign_orders_seen: set[str] = set()
    # Filled entries from a previous run that were never protected. Filled
    # at startup and surfaced on /healthz, because a CRITICAL line at boot
    # scrolls away and this is a question worth answering hours later.
    unprotected_fills: list[str] = []
    background_tasks: set[asyncio.Task[Any]] = set()

    async def on_trade(event: MarketTrade) -> None:
        counters.record_trade(event.symbol)
        market_logger.debug("market trade symbol=%s timestamp=%s price=%s size=%s trade_id=%s",
                     event.symbol, event.timestamp, event.price, event.size,
                     event.trade_id)
        # Off the event loop: firing a rung is a blocking urllib call, and
        # blocking here stalls every other stream on the same loop.
        await asyncio.to_thread(supervisor.on_trade, event)

    async def on_bar(event: MarketBar) -> None:
        counters.record_bar(event.symbol, getattr(event, "trade_count", None))
        market_logger.debug("market bar symbol=%s timestamp=%s o=%s h=%s l=%s c=%s "
                     "volume=%s trades=%s", event.symbol, event.timestamp,
                     event.open, event.high, event.low, event.close,
                     event.volume, event.trade_count)
        await asyncio.to_thread(supervisor.on_bar, event)

    async def on_order_update(event: OrderUpdate) -> None:
        status = f"broker_{event.status or event.event}"
        detail = f"event={event.event}; filled_qty={event.filled_qty}"
        updated = store.update_by_order_id(event.order_id, status, detail)
        logger.info("Alpaca order update order_id=%s event=%s status=%s",
                    event.order_id, event.event, event.status)
        logger.debug("order state symbol=%s side=%s qty=%s filled_qty=%s "
                     "client_order_id=%s", event.symbol, event.side, event.qty,
                     event.filled_qty, event.client_order_id)
        if not updated:
            # The store holds what DIRECT EXECUTION placed — entry, protection
            # generation 0, flatten. Every order the supervisor places after the
            # handoff is absent from it by design, so "not in the store" was
            # never the same question as "not ours". Warning on it fired seven
            # times in one five-minute lot and taught the reader to skip the
            # level, which matters because this account is shared: an order from
            # Wei's other system, or placed by hand, lands here too and is the
            # one thing on this line worth waking up for.
            if is_ours(event.client_order_id):
                logger.debug("supervisor-owned order update client_order_id=%s "
                             "order_id=%s", event.client_order_id, event.order_id)
            elif event.order_id not in foreign_orders_seen:
                # Per order, not per event: one order emits new, partial_fill and
                # fill, which is precisely how a single rung produced three of
                # the old warnings.
                if len(foreign_orders_seen) >= FOREIGN_ORDER_MEMORY:
                    foreign_orders_seen.clear()     # bounded; re-warning is cheap
                foreign_orders_seen.add(event.order_id)
                logger.warning(
                    "FOREIGN order on this account, not placed by this gateway: "
                    "%s %s qty=%s status=%s client_order_id=%s order_id=%s",
                    event.symbol, event.side, event.qty, event.status,
                    event.client_order_id, event.order_id)
        # Route the fill FIRST. Notifying used to come before this line, so a
        # Discord 403 raised past it and the lot never heard about the fill —
        # the primary fill path was dead and it looked like a network problem.
        await asyncio.to_thread(supervisor.on_order_update, event)
        notifier.send(
            f"Alpaca paper order update: {event.side.upper()} {event.qty} {event.symbol}; "
            f"event={event.event}; status={event.status}; filled={event.filled_qty}"
        )

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
                            on_stream_error, resync_orders_after_reconnect,
                            on_bar=on_bar)
        if settings.stream_enabled
        else None
    )

    async def heartbeat_periodically() -> None:
        while True:
            await asyncio.sleep(settings.heartbeat_seconds)
            try:
                await asyncio.to_thread(
                    counters.emit,
                    [*settings.market_symbols, *settings.crypto_symbols])
                await asyncio.to_thread(supervisor.heartbeat)
            except Exception:
                logger.exception("heartbeat failed")

    async def reconcile_periodically() -> None:
        """Alpaca does not replay trade_updates missed while the socket was down.

        A stream that dropped and came back leaves state that looks fine and is
        not, and nothing in the message flow will ever correct it. One REST call
        per open lot per interval is the cheap side of that trade.
        """
        while True:
            await asyncio.sleep(settings.lot_reconcile_seconds)
            try:
                await asyncio.to_thread(supervisor.reconcile_all)
            except Exception:
                logger.exception("periodic lot reconciliation failed")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Before the socket, deliberately. A lot whose stop was cancelled while
        # the process was down is unprotected right now, and the first tick may
        # be a minute away — or, if the connection slot is taken, never.
        try:
            restored = await asyncio.to_thread(supervisor.start)
            if restored:
                logger.info("re-armed %d managed lot(s): %s", len(restored),
                            ", ".join(lot.event_id for lot in restored))
        except Exception:
            logger.exception("could not re-arm managed lots at startup")

        # The net that #71's cancellation handler promised and did not have.
        #
        # A crash between an entry filling and its protection being placed
        # leaves no lot row (written only after protection succeeds), and
        # `broker_filled` is TERMINAL so the reconnect resync skips it. Nothing
        # looked for this state, while a CRITICAL log line told the operator
        # reconciliation would find it.
        #
        # Reported, not auto-repaired. After a restart the stop price is not
        # reliably recoverable, and this account is shared — placing a guessed
        # protective order, or flattening a position another system may own, is
        # its own hazard. Naming the event ids is what the operator needs; the
        # decision is theirs.
        try:
            unprotected_fills.clear()
            unprotected_fills.extend(await asyncio.to_thread(
                store.filled_without_protection))
            for event_id in unprotected_fills:
                logger.critical(
                    "UNPROTECTED FILL from a previous run: %s filled and no "
                    "protective or flatten order was ever recorded. Check the "
                    "position at the broker — re-sending the alert will be "
                    "refused as a duplicate", event_id)
        except Exception:
            logger.exception("could not check for unprotected fills at startup")

        if stream is not None and settings.stream_enabled:
            await stream.start()
        timer = (asyncio.create_task(reconcile_periodically(), name="lot-reconcile")
                 if settings.lot_reconcile_seconds > 0 else None)
        beat = (asyncio.create_task(heartbeat_periodically(), name="lot-heartbeat")
                if settings.heartbeat_seconds > 0 else None)
        try:
            yield
        finally:
            for task in tuple(background_tasks):
                task.cancel()
            if background_tasks:
                await asyncio.gather(*background_tasks, return_exceptions=True)
            for task in (timer, beat):
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            if stream is not None and settings.stream_enabled:
                await stream.stop()

    app = FastAPI(title="TradingView Alpaca Gateway", version="0.1.0", lifespan=lifespan)
    # Exposed so the streams can be inspected and exercised from outside —
    # an end-to-end test needs to trigger a resync without reaching into a
    # closure, and an operator needs a way to see whether a socket is up.
    logger.info("gateway starting: commit=%s paper_trading=%s trading_enabled=%s",
                RUNNING_COMMIT, settings.paper_trading, settings.trading_enabled)
    app.state.stream = stream
    app.state.store = store
    app.state.broker = broker
    app.state.supervisor = supervisor
    app.state.market_counters = counters

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        """Everything needed to answer "why was my order refused?".

        Four failures in one day came from runtime state rather than code: a
        stale checkout, a stale process, a stale environment, and a decision
        that never reached the machine. In each case the repository and the
        agreed configuration were right and the running process was not.

        `commit` closed the first two. The risk settings close the rest — a
        drift like MAX_NOTIONAL still being 2000 after it was raised to 3500
        is then one request away instead of requiring someone to dump a
        process environment on another host.

        Deliberately excluded: the webhook secret, the API keys, and the
        database path. This endpoint is unauthenticated, and "what would help
        me debug" is not sufficient reason to publish a credential.
        """
        # Stream state, because a socket in a reconnect loop is invisible
        # otherwise: the trade-update stream 403'd all day behind log warnings
        # while this endpoint answered ok:true. `ok` now means the streams are
        # carrying data too, not merely that the process is answering HTTP.
        streams = stream.health() if stream is not None else {}
        return {
            "ok": all(v == "connected" for v in streams.values()) if streams else True,
            "streams": streams,
            "paper_trading": settings.paper_trading,
            "trading_enabled": settings.trading_enabled,
            # Entries that filled in a previous run and were never protected.
            # A CRITICAL line at boot scrolls away; this is a question worth
            # answering hours later, from another machine, without log access.
            "unprotected_fills": list(unprotected_fills),
            "commit": RUNNING_COMMIT,
            # True means the running code differs from that commit.
            "worktree_dirty": WORKTREE_DIRTY,
            "risk": {
                "allowed_symbols": sorted(settings.allowed_symbols),
                "max_qty": settings.max_qty,
                "crypto_max_qty": str(settings.crypto_max_qty),
                "max_notional": settings.max_notional,
                "max_price_deviation": settings.max_price_deviation,
                "max_alert_age_seconds": settings.max_alert_age_seconds,
            },
        }

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

    @app.post("/webhooks/tradingview/pine/submit", status_code=202, response_model=None)
    async def pine_submit(
        request: Request,
        x_tv_secret: str | None = Header(default=None),
        x_delivery_id: str | None = Header(default=None),
        x_discord_message_id: str | None = Header(default=None),
    ) -> JSONResponse | dict[str, Any]:
        """Authenticate, parse, and hand the command to the execution engine.

        Deliberately thin. An earlier draft did its own parse -> risk -> claim
        -> submit, which created a second execution path where only the engine
        was covered by the Stage 3 contract — so the lifecycle rules were
        enforced on the path nobody used and absent from the one wired to a
        route. Everything between parsing and the broker belongs to
        execute_pine_command, and this route's job is to stop being clever.
        """
        if not settings.webhook_secret or not x_tv_secret or not hmac.compare_digest(
                x_tv_secret, settings.webhook_secret):
            raise HTTPException(status_code=401, detail="invalid webhook secret")
        try:
            raw = (await _read_limited_pine_body(request)).decode("utf-8")
            command = parse_pine_alert(raw)
        except (UnicodeDecodeError, PineAlertParseError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        delivery_id = x_delivery_id or x_discord_message_id
        try:
            submission = await asyncio.to_thread(
                execution.submit_pine_entry, command, settings, broker, store,
                delivery_id=delivery_id, supervisor=supervisor)
        except ExecutionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("submission failed for %s", command.event_id)
            raise HTTPException(status_code=502, detail="broker submission failed") from exc

        if isinstance(submission, execution.ExecutionResult):
            return JSONResponse(status_code=200, content={
                "accepted": submission.entry_status == "duplicate",
                "event_id": command.event_id or delivery_id,
                "order_id": submission.entry_order_id,
                "entry_status": submission.entry_status,
                "protection_status": submission.protection_status,
            })

        async def finish_in_background() -> None:
            try:
                result = await asyncio.to_thread(
                    execution.finish_pine_execution, submission)
                logger.info(
                    "Pine execution complete event_id=%s entry_order_id=%s "
                    "entry_status=%s protection_order_id=%s protection_status=%s",
                    submission.event_id, result.entry_order_id, result.entry_status,
                    result.protection_order_id, result.protection_status)
                try:
                    notifier.send(
                        f"Pine order complete: {command.side.upper()} {command.qty} "
                        f"{command.symbol}; entry={result.entry_order_id}; "
                        f"protection={result.protection_status}"
                    )
                except Exception:
                    logger.exception("completion notification failed for %s", submission.event_id)
            except UnprotectedPositionError as exc:
                # The worst state this system can reach, and it must not be
                # reported like a transient failure. Synchronously this raised
                # out of the request and the caller learned at once; the
                # acknowledgement took that away, so what is left has to carry
                # the weight on its own.
                #
                # Recorded as well as logged, deliberately. A log line does not
                # survive a rotation or a restart, and this is the one state an
                # operator must still be able to find tomorrow morning.
                logger.critical(
                    "UNPROTECTED POSITION after acknowledgement: %s %s %s "
                    "event_id=%s entry_order_id=%s — %s",
                    command.side, command.qty, command.symbol,
                    submission.event_id, exc.entry_order_id, exc)
                with contextlib.suppress(Exception):
                    store.update(submission.event_id, "unprotected_and_open",
                                 str(exc), broker_order_id=exc.entry_order_id)
                with contextlib.suppress(Exception):
                    notifier.send(
                        f"UNPROTECTED POSITION: {command.side.upper()} "
                        f"{command.qty} {command.symbol} is open and could not "
                        f"be protected or closed (entry={exc.entry_order_id})")
            except asyncio.CancelledError:
                # CancelledError is a BaseException, so the generic arm below
                # never saw it: a shutdown between fill and stop placement
                # produced no output at all. Worse, the work is in
                # `asyncio.to_thread`, so cancelling the task does not stop the
                # thread — whether the stop was placed is genuinely unknown
                # from here, and saying so is the only honest option.
                logger.critical(
                    "protection was CANCELLED before it completed event_id=%s "
                    "entry_order_id=%s; the worker thread may or may not have "
                    "placed a stop. The next start reports this as an "
                    "UNPROTECTED FILL and lists it on /healthz — it does not "
                    "repair it, so check the position at the broker",
                    submission.event_id, submission.entry_order_id)
                with contextlib.suppress(Exception):
                    store.update(submission.event_id, "protection_cancelled",
                                 "shutdown before protection completed",
                                 broker_order_id=submission.entry_order_id)
                raise
            except Exception:
                logger.exception(
                    "background Pine execution failed event_id=%s entry_order_id=%s",
                    submission.event_id, submission.entry_order_id)

        task = asyncio.create_task(
            finish_in_background(), name=f"pine-finish-{submission.event_id}")
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

        try:
            notifier.send(
                f"Pine order accepted: {command.side.upper()} {command.qty} "
                f"{command.symbol}; entry={submission.entry_order_id}; "
                "protection=pending"
            )
        except Exception:
            logger.exception("acceptance notification failed for %s", submission.event_id)

        return JSONResponse(status_code=202, content={
            "accepted": True,
            "event_id": command.event_id or delivery_id,
            "order_id": submission.entry_order_id,
            "entry_order_id": submission.entry_order_id,
            "entry_status": submission.entry_status,
            "protection_status": "pending",
        })

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
