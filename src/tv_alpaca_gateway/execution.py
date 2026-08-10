"""Turn one parsed Pine command into orders, and keep the position protected.

The order of operations here is the whole design, and every step of it comes
from something measured against Alpaca paper rather than reasoned about:

    validate config, kill switch, allowlist, size, notional
    submit the entry
    if it did not fill, cancel it at the deadline and stop
    read the HELD position from the broker
    place protection sized from that position, retrying once
    if protection cannot be placed, flatten from the position
    if flattening also fails, say so unmistakably

WHY PROTECTION IS SIZED FROM THE POSITION, NOT THE FILL
-------------------------------------------------------
Alpaca charges the crypto fee in kind. Measured: a filled 0.001 BTC leaves a
position of 0.0009975. A protective stop sized to `filled_qty` asks to sell
more than is held, the broker refuses it, and the position is left unprotected
while the log says protection was placed. So the quantity is read back from the
broker, never computed as ``filled_qty * (1 - fee)``, which drifts with any fee
schedule or partial fill.

WHY FAILING TO PROTECT MEANS FLATTENING
---------------------------------------
An unprotected position is worse than no position: the strategy believes its
risk is bounded and it is not. Wei chose retry-once-then-flatten, which
converts an unattended open-ended exposure into a realised loss of known size.
The flatten is sized from the position for the same reason the stop is.

WHAT THE ASSET CLASS CHANGES
----------------------------
Measured against Alpaca paper:

    trailing_stop on BTC/USD  -> "invalid order type for crypto order"
    stop        on BTC/USD    -> "invalid order type for crypto order"
    stop_limit  on BTC/USD    -> accepted, and requires BOTH prices
    trailing_stop on QQQ      -> accepted

So crypto protection is always a stop-limit, and a trailing stop is an equity
feature. Tightening crypto must not remove it from QQQ, which is where the
strategy actually runs.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from . import assets, exit_manager, exit_plans
from .config import Settings
from .pine_alert_parser import PineOrderCommand
from .store import EventStore

logger = logging.getLogger(__name__)

# Protective orders outlive the session. The entry's time-in-force describes
# how the ENTRY should behave; a `day` stop expires at the close and leaves an
# overnight position unprotected while the log still says a stop was placed.
PROTECTION_TIME_IN_FORCE = "gtc"

# One retry. A protective order can fail transiently — a momentary rejection, a
# position not yet settled broker-side — and one retry costs a round trip where
# not retrying costs an unprotected position for a fault that would have
# cleared on its own.
PROTECTION_ATTEMPTS = 2


class ExecutionError(RuntimeError):
    """Refused before anything reached the broker."""


class UnprotectedPositionError(RuntimeError):
    """Filled, exposed, and neither protected nor closed.

    The worst state this system can reach, and the one that must never be
    mistaken for success. Carries the entry id so a caller can act rather than
    go looking in the database.
    """

    def __init__(self, message: str, entry_order_id: str | None = None):
        super().__init__(message)
        self.entry_order_id = entry_order_id


@dataclass(frozen=True)
class ExecutionResult:
    entry_order_id: str | None
    protection_order_id: str | None = None
    entry_status: str | None = None
    protection_status: str | None = None


def _command_id(command: PineOrderCommand, delivery_id: str | None = None) -> str:
    """Identity comes from EVENT_ID, or a delivery id, and never from the order.

    Hashing the order fields made two firings of the same setup produce one id,
    so the second was refused as a duplicate — inverting what idempotency is
    for. A strategy emitting identical signals is the normal case.

    EVENT_ID is optional in the alert, so it can be absent. When it is, the
    caller must supply something durable — the relay's Discord message
    snowflake, say. Falling back to the order fields would restore the original
    bug, and falling back to nothing produces `pine-exec-None` for EVERY alert
    of every symbol, which is worse: the first order ever placed succeeds and
    all others are refused as duplicates.

    So an unidentifiable command is refused. That is a safe failure; a shared
    identity is not.
    """
    identity = command.event_id or delivery_id
    if not identity:
        raise ExecutionError(
            "this alert carries no EVENT_ID and no delivery id was supplied, "
            "so two firings could not be told apart; add "
            "EVENT_ID={{ticker}}-{{interval}}-{{time}} to the alert or pass a "
            "durable delivery id")
    return f"pine-exec-{identity}"


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def execute_pine_command(
    command: PineOrderCommand,
    settings: Settings,
    broker: Any,
    store: EventStore,
    *,
    deadline_seconds: float = 60.0,
    poll_interval: float = 1.0,
    delivery_id: str | None = None,
    supervisor: Any = None,
) -> ExecutionResult:
    settings.validate()
    if not settings.trading_enabled:
        # Recorded, not merely logged. A refusal that leaves no trace is
        # indistinguishable from an alert that never arrived — both look like
        # an empty database and a quiet log, which is exactly the ambiguity
        # that made a blocked alert unprovable.
        #
        # The kill switch is the last line of defence, and an audit that
        # cannot say what it stopped is not an audit. If it were mis-set for a
        # week, nothing would show what failed to trade.
        event_id = _command_id(command, delivery_id)
        store.record_refusal(
            event_id, "kill_switch",
            f"TRADING_ENABLED=false; {command.side} {command.qty} "
            f"{command.symbol} was not submitted")
        logger.warning("kill switch engaged: refused %s %s %s (event %s)",
                       command.side, command.qty, command.symbol, command.event_id)
        return ExecutionResult(None, entry_status="kill_switch")

    symbol = assets.resolve(command.symbol, settings.allowed_symbols)
    if symbol not in settings.allowed_symbols:
        raise ExecutionError(f"{symbol} is not allowlisted")

    crypto = assets.is_crypto(symbol)
    configured_max = settings.crypto_max_qty if crypto else Decimal(settings.max_qty)
    if configured_max <= 0:
        # Named as configuration, not as a risk limit. Reporting "notional
        # exceeds limit" here sent an operator to raise MAX_NOTIONAL, which
        # could never help, because the real cause was an unset size.
        raise ExecutionError(
            f"no order size configured for {symbol} "
            f"({'CRYPTO_MAX_QTY' if crypto else 'MAX_QTY'} is not set)")
    if command.qty > configured_max:
        raise ExecutionError(
            f"requested {command.qty} exceeds the configured maximum {configured_max}")

    reference_price = _decimal(broker.latest_trade_price(symbol))
    if command.qty * reference_price > _decimal(settings.max_notional):
        raise ExecutionError("notional exceeds configured limit")

    # One lot at a time per symbol — Wei's rule, and on crypto it is also
    # forced: the open lot's stop is a resting sell, and a resting sell makes
    # Alpaca refuse the entry buy at submission with NO order record at all.
    # Refusing here produces a reason; letting it through produces silence.
    open_lot_id = store.open_lot_for(symbol)
    if open_lot_id and command.exit_plan:
        store.record_refusal(command.event_id or "", "lot_already_open",
                             f"{symbol} is managed by lot {open_lot_id}")
        raise ExecutionError(
            f"{symbol} already has an open managed lot ({open_lot_id}); "
            f"one lot at a time. Close it or let its ladder finish first")

    event_id = _command_id(command, delivery_id)
    if not store.claim(event_id):
        logger.info("duplicate delivery of %s; not resubmitting", command.event_id)
        return ExecutionResult(None, entry_status="duplicate")

    # Read the position BEFORE the entry. Protection covers what THIS entry
    # added, and the only way to know that without assuming a fee rate is to
    # measure the difference across the fill.
    position_before = _decimal(broker.position_qty(symbol))

    entry = _submit_entry(command, symbol, event_id, broker, store)
    entry_id, entry_status = entry

    if entry_status != "filled":
        # Wait it out. This may observe a fill, in which case the entry is
        # filled and must be protected exactly like one that filled instantly —
        # returning here was a real bug: an order accepted as `new` that filled
        # a second later got no protective stop at all.
        entry_status = _await_fill_or_cancel(
            command, entry_id, entry_status, event_id, broker, store,
            deadline_seconds, poll_interval)

    if entry_status != "filled":
        return ExecutionResult(entry_id, entry_status=entry_status)

    if not command.place_protective_stop_after_fill and command.exit_plan != "OCO_AFTER_FILL":
        return ExecutionResult(entry_id, entry_status=entry_status)

    return _protect_or_flatten(command, symbol, crypto, entry_id, entry_status,
                               event_id, broker, store, position_before, settings,
                               supervisor)


def _submit_entry(command, symbol, event_id, broker, store) -> tuple[str, str]:
    kwargs = {
        "symbol": symbol,
        "qty": command.qty,
        "side": command.side,
        "type": "market",
        "time_in_force": command.time_in_force,
        "client_order_id": event_id,
    }
    try:
        entry = broker.submit_order(**kwargs)
    except Exception as exc:
        # Release the claim: the order was never placed, so a retry of the same
        # alert is both safe and necessary. "Already claimed" must not be
        # conflated with "already submitted".
        store.update(event_id, "failed", str(exc))
        store.release(event_id)
        raise

    entry_id = str(entry["id"])
    status = str(entry.get("status", "unknown"))
    store.update(event_id, f"broker_{status}", broker_order_id=entry_id)
    store.record_broker_order(entry_id, event_id, "entry", status)
    return entry_id, status


def _await_fill_or_cancel(command, entry_id, entry_status, event_id, broker,
                          store, deadline_seconds, poll_interval) -> str:
    """Wait out the deadline; return the status the entry ended up in.

    Returns a STATUS rather than a result, because the caller must decide what
    happens next. An earlier version returned an ExecutionResult directly, so
    an entry that filled during polling skipped protection entirely — filled,
    exposed, and no stop, which is the precise outcome this module exists to
    prevent. Found in review by TradingBot; reproduced before fixing.
    """
    # Waiting for the fill and cancelling at the deadline are DIFFERENT
    # questions, and conflating them is what left a filled position
    # unprotected in production.
    #
    # Alpaca answers a crypto market order with `accepted`, not `filled`. With
    # protection requested but no CANCEL_UNFILLED_AT_DEADLINE, this returned
    # immediately, execute_pine_command saw a non-filled status and returned
    # before _protect_or_flatten — and the order then filled asynchronously at
    # the broker. Entry filled, recorded, no stop, and every other signal
    # looked like success.
    #
    # So: wait whenever the outcome depends on knowing the fill happened.
    # Cancel only when the alert asked for it.
    needs_fill = (command.place_protective_stop_after_fill
                  or command.exit_plan == "OCO_AFTER_FILL")
    if not (command.cancel_unfilled_at_deadline or needs_fill):
        return entry_status

    deadline = time.monotonic() + max(deadline_seconds, 0.0)
    status = entry_status
    while time.monotonic() < deadline:
        time.sleep(min(poll_interval, max(deadline - time.monotonic(), 0.0)))
        try:
            current = broker.get_order(entry_id)
        except Exception:
            logger.exception("could not poll %s while waiting for a fill", entry_id)
            break
        status = str(current.get("status", status)) if isinstance(current, dict) \
            else str(getattr(current, "status", status))
        if status == "filled":
            store.update(event_id, "broker_filled", broker_order_id=entry_id)
            return "filled"

    if not command.cancel_unfilled_at_deadline:
        # We waited only to learn whether it filled. It did not, and the alert
        # never asked for a cancellation — leaving it working is what was
        # requested, and the caller is told it is still unfilled.
        logger.warning("%s still unfilled after %.0fs and no cancellation was "
                       "requested; it remains working and unprotected",
                       entry_id, deadline_seconds)
        return status

    try:
        broker.cancel_order(entry_id)
        store.update(event_id, "broker_canceled", "unfilled at deadline",
                     broker_order_id=entry_id)
        logger.info("cancelled %s: unfilled after %.0fs", entry_id, deadline_seconds)
    except Exception as exc:
        # It may have filled in the race between the last poll and the cancel.
        logger.warning("could not cancel %s at the deadline: %s", entry_id, exc)
        store.update(event_id, "cancel_failed", str(exc), broker_order_id=entry_id)
        return status
    return "canceled"


def _protection_kwargs(command, symbol, crypto, held_qty, event_id) -> dict:
    kwargs = {
        "symbol": symbol,
        "qty": held_qty,
        "side": "sell" if command.side == "buy" else "buy",
        "time_in_force": PROTECTION_TIME_IN_FORCE,
        "client_order_id": f"{event_id}-protection",
    }
    if not crypto and command.trail is not None:
        # trailing_stop is an equity feature; Alpaca refuses it on crypto.
        kwargs.update({"type": "trailing_stop", "trail_price": command.trail})
    else:
        kwargs.update({
            "type": "stop_limit",
            "stop_price": command.stop_trigger,
            "limit_price": command.stop_limit,
        })
    return kwargs


def _protect_or_flatten(command, symbol, crypto, entry_id, entry_status,
                        event_id, broker, store,
                        position_before: Decimal = Decimal("0"),
                        settings=None, supervisor=None) -> ExecutionResult:
    """Protect what THIS entry added, not the whole position.

    Sizing from the total position was wrong twice over.

    It is not what was asked for: a stop belongs to the entry that requested
    it, and covering unrelated holdings in the same symbol means one alert can
    close a position another strategy opened.

    And it does not work more than once. A resting stop RESERVES quantity —
    measured on the live account, 0.00648125 held with only 0.00498125
    available — so a second alert sized to the total would ask to sell more
    than is available and be refused. The first protective alert would succeed
    and every one after it would fail.

    The quantity is still measured rather than computed: position after minus
    position before. That keeps the in-kind fee handled without assuming a fee
    rate, which is the part that made total-position sizing attractive in the
    first place.
    """
    position_after = _decimal(broker.position_qty(symbol))
    held_qty = position_after - position_before
    if held_qty <= 0:
        logger.warning(
            "%s position did not increase after a filled entry (%s -> %s); "
            "nothing to protect", symbol, position_before, position_after)
        return ExecutionResult(entry_id, entry_status=entry_status)

    if command.exit_plan:
        # One plan name, best available mechanism for the asset class.
        #
        #   equity  -> Alpaca's native OCO. The broker holds both legs, so the
        #              pair survives this process dying, which software
        #              management never can.
        #   crypto  -> managed here. Alpaca has no native OCO for crypto at
        #              all, so the alternative is not a worse OCO, it is none.
        #
        # Dispatching on the asset rather than refusing keeps the API's
        # limitation out of the strategy: Wei writes one plan name and gets
        # the strongest thing available for the symbol he wrote it on.
        if command.exit_plan == "OCO_AFTER_FILL" and not crypto:
            return _submit_oco_exit(command, symbol, entry_id, entry_status,
                                    event_id, broker, store, held_qty)
        managed = _open_managed_lot(command, symbol, entry_id, entry_status,
                                    event_id, broker, store, held_qty, settings,
                                    supervisor)
        if managed is not None:
            return managed
        # Falling through is deliberate. A ladder that cannot be built is a
        # reason to place the ordinary stop, not a reason to leave the position
        # naked or to flatten a fill the user wanted.
        logger.warning("exit plan %s could not be armed for %s; falling back "
                       "to a single protective stop", command.exit_plan, symbol)

    kwargs = _protection_kwargs(command, symbol, crypto, held_qty, event_id)
    last_error: Exception | None = None
    for attempt in range(1, PROTECTION_ATTEMPTS + 1):
        try:
            protection = broker.submit_order(**kwargs)
        except Exception as exc:
            last_error = exc
            logger.warning("protective order attempt %d/%d failed: %s",
                           attempt, PROTECTION_ATTEMPTS, exc)
            continue
        protection_id = str(protection["id"])
        # The protective order's id is recorded too. Without it a reconnect
        # resync can find the entry and not the stop, so an order that exists
        # at the broker is invisible to reconciliation.
        store.update(event_id, "protection_submitted",
                     f"protection_order_id={protection_id}",
                     broker_order_id=entry_id)
        # Recorded as its own row: reconciliation must be able to find a
        # protective order, and a missed one means an unprotected position.
        store.record_broker_order(protection_id, event_id, "protection",
                                  str(protection.get("status", "new")))
        return ExecutionResult(entry_id, protection_id, entry_status, "submitted")

    return _flatten(command, symbol, entry_id, entry_status, event_id, broker,
                    store, last_error, held_qty)


def _submit_oco_exit(command, symbol, entry_id, entry_status, event_id, broker,
                     store, held_qty) -> ExecutionResult:
    """Submit one native Alpaca OCO exit for the delta filled by this entry."""
    try:
        order = broker.submit_order(
            symbol=symbol, qty=held_qty,
            side="sell" if command.side == "buy" else "buy",
            type="limit", time_in_force=PROTECTION_TIME_IN_FORCE,
            order_class="oco",
            take_profit_limit_price=command.take_profit,
            stop_loss_stop_price=command.stop_trigger,
            stop_loss_limit_price=command.stop_limit,
            client_order_id=f"{event_id}-oco")
    except Exception as exc:
        return _flatten(command, symbol, entry_id, entry_status, event_id, broker,
                        store, exc, held_qty)
    protection_id = str(order["id"])
    store.update(event_id, "protection_submitted",
                 f"protection_order_id={protection_id}", broker_order_id=entry_id)
    store.record_broker_order(protection_id, event_id, "protection",
                              str(order.get("status", "new")))
    return ExecutionResult(entry_id, protection_id, entry_status, "submitted")


def _open_managed_lot(command, symbol, entry_id, entry_status, event_id,
                      broker, store, held_qty, settings=None,
                      supervisor=None) -> ExecutionResult | None:
    """Hand this entry to the exit manager. None means it could not be armed.

    The disaster stop is placed by `open_lot`, so this REPLACES the ordinary
    protective order rather than adding to it. Two resting sells for the same
    coins is not double protection — the second is refused for want of
    available quantity, and on crypto both then block the next entry.

    The entry price comes from the broker's `filled_avg_price`, never from the
    signal: R is entry minus stop, and a market order into a fast tape does not
    fill where the alert fired.
    """
    # A lot on a symbol nobody subscribed to is the quietest failure in this
    # system. ALLOWED_SYMBOLS says what may be traded; MARKET_SYMBOLS and
    # CRYPTO_SYMBOLS say what the sockets listen to, and nothing reconciled
    # them. The ladder would arm, the disaster stop would rest, every order
    # would look right — and no bar would ever arrive, so the runner would
    # never trail. Falling back to an ordinary stop is honest about that;
    # pretending to manage a lot we cannot see is not.
    if settings is not None:
        streamed = {assets.normalise(t) for t in
                    (*settings.market_symbols, *settings.crypto_symbols)}
        if assets.normalise(symbol) not in streamed:
            store.record_refusal(
                command.event_id or "", "symbol_not_streamed",
                f"{symbol} is tradable but not subscribed; add it to "
                f"{'CRYPTO_SYMBOLS' if assets.is_crypto(symbol) else 'MARKET_SYMBOLS'}")
            logger.error(
                "%s is in ALLOWED_SYMBOLS but not in the stream subscription, so "
                "no bars will arrive and the runner could never trail; placing an "
                "ordinary protective stop instead of an exit plan", symbol)
            return None

    try:
        entry_price = broker.fill_price(entry_id)
        if not entry_price:
            raise ExecutionError(
                f"no fill price for {entry_id}; R cannot be measured and every "
                f"target would sit at the wrong distance from the real risk")
        lot = exit_manager.Lot.opened(
            event_id=event_id, symbol=symbol, entry_price=entry_price,
            initial_stop=command.stop_trigger, held_qty=held_qty,
            timeframe=command.interval, plan=exit_plans.resolve(command.exit_plan),
            min_order_size=broker.min_order_size(symbol))
        exit_manager.open_lot(lot, broker)
    except Exception as exc:
        logger.warning("could not arm exit plan %s on %s: %s",
                       command.exit_plan, symbol, exc)
        return None

    # Hand it to the RUNNING supervisor, not just to the database.
    #
    # Without this the lot is armed at the broker and recorded on disk, and the
    # live process has never heard of it: every price tick looks the symbol up,
    # finds nothing, and does nothing. The ladder only came alive after a
    # restart, when start() reloaded it from the store — so a take-profit that
    # was already breached simply never fired.
    #
    # Observed in production before it was found in a test, which is the part
    # worth remembering: the contract tests called supervisor.adopt() in their
    # own setup, so they performed the exact wiring step the real path omits.
    # A fixture that does the work under test cannot fail when the work is
    # missing.
    if supervisor is not None:
        supervisor.adopt(lot)          # persists as part of remembering it
    else:
        store.save_lot(event_id, symbol, lot.stage, exit_manager.dump_lot(lot))
    store.update(event_id, "lot_opened",
                 f"plan={command.exit_plan} stop={lot.stop_order_id}",
                 broker_order_id=entry_id)
    if lot.stop_order_id:
        store.record_broker_order(lot.stop_order_id, event_id, "protection", "new")
    return ExecutionResult(entry_id, lot.stop_order_id, entry_status, "lot_opened")


def _flatten(command, symbol, entry_id, entry_status, event_id, broker, store,
             last_error, held_qty: Decimal | None = None) -> ExecutionResult:
    """Close a position that cannot be protected.

    Sized from the broker's position for the same reason the stop is: a close
    sized from the fill asks to sell more than is held, is refused, and the
    position survives the mechanism meant to end it.
    """
    store.update(event_id, "protection_failed", str(last_error),
                 broker_order_id=entry_id)
    # Close what this entry added, not the whole position — flattening
    # someone else's holdings to unwind our own failure would be worse than
    # the failure.
    if held_qty is None:
        held_qty = _decimal(broker.position_qty(symbol))
    if held_qty <= 0:
        return ExecutionResult(entry_id, entry_status=entry_status,
                               protection_status="failed_no_position")

    logger.error("could not protect %s; flattening %s", symbol, held_qty)
    try:
        closing = broker.submit_order(
            symbol=symbol, qty=held_qty,
            side="sell" if command.side == "buy" else "buy",
            type="market", time_in_force=PROTECTION_TIME_IN_FORCE,
            client_order_id=f"{event_id}-flatten")
    except Exception as exc:
        store.update(event_id, "unprotected_and_open", str(exc),
                     broker_order_id=entry_id)
        logger.critical(
            "UNPROTECTED POSITION: %s %s could not be protected or closed (%s)",
            symbol, held_qty, exc)
        raise UnprotectedPositionError(
            f"{symbol} {held_qty} is open, unprotected, and could not be "
            f"closed: {exc}", entry_order_id=entry_id) from exc

    flatten_id = str(closing.get("id", ""))
    store.record_broker_order(flatten_id, event_id, "flatten",
                              str(closing.get("status", "new")))
    store.update(event_id, "flattened_unprotected",
                 f"flatten_order_id={flatten_id}; {last_error}",
                 broker_order_id=entry_id)
    return ExecutionResult(entry_id, entry_status=entry_status,
                           protection_status="flattened")
