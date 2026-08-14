"""Routes market and order events into the lots that care about them.

`exit_manager.Lot` decides; this decides *which* lot is asked. Keeping the two
apart is what lets the ladder be tested without a socket, and it is also the
seam where the interesting mistakes live — a fill routed to the wrong rung is
indistinguishable, from inside the lot, from a fill that really happened.

Every handler persists after it acts. The window between deciding and writing
is the window a crash loses, and what it loses is the record of an order that
already exists at the broker.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal

from . import assets
from .exit_manager import Lot, _prefixed, dump_lot, load_lot, reconcile_lot
from .market_log import logger as market_logger

logger = logging.getLogger(__name__)

# pine-exec-<event id>-tp<rung>[r<attempt>]
_RUNG = re.compile(r"^pine-exec-(?P<event>.+)-tp(?P<rung>\d+)(?:r\d+)?$")


def rung_of(client_order_id: str) -> tuple[str, int] | None:
    """(event id, rung) for a take-profit order, or None if it is not one.

    Matched on the whole id, not by `in`: the protective order for event
    `abc-tp1` would otherwise look like a rung of event `abc`.

    The event id is returned exactly as the client order id spells it. Whether
    it carries the `pine-exec-` prefix depends on how the lot was created, so
    the COMPARISON normalises both sides rather than this function guessing —
    guessing here broke every lot whose event_id had no prefix.

    The mismatch it fixes was hidden by another bug. Client order ids used to double
    the prefix (`pine-exec-pine-exec-…`), so stripping one still left the other
    and the comparison matched by accident. Removing the doubling made the
    stripping visible, and every fill started arriving as:

        INFO fill for unmanaged lot btc-… (BTC/USD)

    Live consequence: rung fills stopped routing entirely, and the ladder ran
    on the 60-second reconcile timer instead. Correct, and a minute late.
    """
    match = _RUNG.match(client_order_id or "")
    return (match["event"], int(match["rung"])) if match else None


def fill_identity(update) -> str:
    """A stable id for one execution.

    Alpaca puts `execution_id` on a trade_update; when it is missing, the order
    id plus the cumulative filled quantity is still unique per execution,
    because that quantity only ever increases. Falling back to the order id
    alone would collapse every partial into one and drop all but the first.
    """
    data = (getattr(update, "raw", None) or {}).get("data") or {}
    execution = data.get("execution_id")
    return str(execution) if execution else f"{update.order_id}:{update.filled_qty}"


class LotSupervisor:
    """Owns the open lots and feeds them."""

    def __init__(self, store, broker):
        self.store = store
        self.broker = broker
        self.lots: dict[str, Lot] = {}          # keyed by normalised symbol

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> list[Lot]:
        """Re-arm from the database, then check every lot against the broker.

        Reconciliation is not optional here and not deferred to the first tick:
        a lot whose stop was cancelled while the process was down is
        unprotected right now, and the first tick may be a minute away.
        """
        restored = []
        for event_id, symbol, state in self.store.open_lots():
            try:
                lot = reconcile_lot(load_lot(state), self.broker)
            except Exception:
                # A lot that cannot be rebuilt is a position with no manager.
                # Log it loudly and keep going: one unreadable row must not
                # stop the other lots being re-protected.
                logger.exception("could not restore lot %s on %s", event_id, symbol)
                continue
            self._remember(lot)
            self._seed_trail(lot)
            restored.append(lot)
        return restored

    def _seed_trail(self, lot) -> None:
        """Replay recent completed bars so a restarted runner trails from the
        right place.

        Without it the stop sits at its last persisted value until a live bar
        arrives — up to a minute on 1m, and indefinitely if the one
        market-data connection slot is held by another process. The lot would
        look managed and would not be.

        Only the runner stage needs it; before that the trail is not moving.
        Bars go through `on_bar`, so the no-trades rule applies here too rather
        than being reimplemented — a quote-only bar must not seed a stop at a
        price nothing traded at, and least of all at startup where there is
        nothing to correct it.
        """
        if lot.stage != "runner":
            return
        try:
            bars = self.broker.recent_bars(lot.symbol, lot.timeframe)
        except Exception:
            # Startup must survive a market-data outage. A lot with a stale
            # trail is worse than one with a fresh trail and better than a
            # gateway that will not start at all.
            logger.exception("could not seed the trail for lot %s; it will "
                             "trail from its stored stop", lot.event_id)
            return
        for bar in bars:
            lot.on_bar(high=Decimal(str(bar.high)), low=Decimal(str(bar.low)),
                       close=Decimal(str(bar.close)),
                       trade_count=getattr(bar, "trade_count", None))
        if bars:
            logger.info("seeded lot %s with %d bar(s); working stop %s",
                        lot.event_id, len(bars), lot.working_stop)
        self._remember(lot)

    def adopt(self, lot: Lot) -> Lot:
        self._remember(lot)
        return lot

    def _remember(self, lot: Lot) -> None:
        key = assets.normalise(lot.symbol)
        if lot.is_closed:
            self.lots.pop(key, None)
        else:
            self.lots[key] = lot
        self.store.save_lot(lot.event_id, lot.symbol, lot.stage, dump_lot(lot))

    def _for(self, symbol: str) -> Lot | None:
        return self.lots.get(assets.normalise(symbol))

    # ── market events ───────────────────────────────────────────────────────

    def on_bar(self, bar) -> None:
        lot = self._for(bar.symbol)
        if lot is None:
            # Market data, not a state transition — and it fires for every bar
            # on every symbol with no lot, so it is loudest when nothing is
            # happening. Same reasoning #51 applied to the trade path.
            market_logger.debug("lot state=none event=bar symbol=%s timestamp=%s",
                                bar.symbol, getattr(bar, "timestamp", None))
            return
        before = (lot.stage, lot.working_stop, lot.remaining_qty,
                  tuple(sorted(lot.filled_rungs)))
        lot.on_bar(high=bar.high, low=bar.low, close=bar.close,
                   trade_count=bar.trade_count)
        self._remember(lot)
        after = (lot.stage, lot.working_stop, lot.remaining_qty,
                 tuple(sorted(lot.filled_rungs)))
        # Only when the bar actually changed something. A bar that moves
        # nothing is market data, and on this feed most of them move nothing.
        if after != before:
            logger.debug("lot state=after_bar event_id=%s symbol=%s stage=%s->%s "
                         "working_stop=%s->%s remaining=%s->%s filled_rungs=%s "
                         "bar_trades=%s", lot.event_id, lot.symbol, before[0],
                         lot.stage, before[1], lot.working_stop, before[2],
                         lot.remaining_qty, sorted(lot.filled_rungs),
                         bar.trade_count)

    def on_trade(self, trade) -> None:
        lot = self._for(trade.symbol)
        if lot is None:
            return
        before = (lot.stage, lot.working_stop, lot.remaining_qty,
                  tuple(sorted(lot.filled_rungs)),
                  tuple(sorted(lot.pending_rungs)), lot.breakeven_pending)
        lot.on_price(Decimal(str(trade.price)))
        self._remember(lot)
        after = (lot.stage, lot.working_stop, lot.remaining_qty,
                 tuple(sorted(lot.filled_rungs)),
                 tuple(sorted(lot.pending_rungs)), lot.breakeven_pending)
        # A raw trade is market data, not a state transition. The market logger
        # owns optional raw-data output; this logger only reports a meaningful
        # decision/change so DEBUG remains readable during a busy tape.
        if after != before:
            logger.debug("lot state=after_trade event_id=%s symbol=%s price=%s "
                         "stage=%s->%s working_stop=%s->%s remaining=%s->%s "
                         "filled_rungs=%s pending_rungs=%s breakeven_pending=%s",
                         lot.event_id, lot.symbol, trade.price, before[0],
                         lot.stage, before[1], lot.working_stop, before[2],
                         lot.remaining_qty, sorted(lot.filled_rungs),
                         sorted(lot.pending_rungs), lot.breakeven_pending)

    # ── order events ────────────────────────────────────────────────────────

    def on_order_update(self, update) -> None:
        """Route a fill to the rung that caused it.

        Only fills are routed. A `new` or `accepted` update for the same order
        carries a filled quantity of zero and would otherwise be counted as an
        execution of nothing, consuming the dedupe id that the real fill needs.
        """
        logger.debug("lot order event=%s order_id=%s client_order_id=%s "
                     "symbol=%s status=%s filled_qty=%s", getattr(update, "event", None),
                     update.order_id, update.client_order_id, update.symbol,
                     getattr(update, "status", None), update.filled_qty)
        if not update.is_fill:
            return
        identified = rung_of(update.client_order_id)
        if identified is None:
            # The disaster stop firing is the one event that ends a lot without
            # the lot deciding anything. Left to the timer it would be up to a
            # reconcile interval before the gateway noticed the position was
            # gone — and during that window it would still be arming rungs
            # against coins that had already been sold.
            if "-protection-" in (update.client_order_id or ""):
                lot = self._for(update.symbol)
                if lot is not None:
                    self._remember(reconcile_lot(lot, self.broker))
            return
        event_id, rung = identified
        lot = self._for(update.symbol)
        # Both sides normalised. A lot's event_id may or may not carry the
        # `pine-exec-` prefix depending on how it was created, and the client
        # order id always does — comparing them raw dropped every fill as
        # "unmanaged", live, for as long as the ids were not accidentally
        # symmetrical.
        if lot is None or _prefixed(lot.event_id) != _prefixed(event_id):
            # A fill for a lot this process does not hold. Not an error — it may
            # belong to a closed lot — but worth seeing, because the other way
            # it happens is a routing bug.
            logger.info("fill for unmanaged lot %s (%s)", event_id, update.symbol)
            return
        filled = Decimal(str(update.filled_qty or "0")) - lot.rung_filled_qty.get(
            rung, Decimal("0"))
        if filled <= 0:
            return          # cumulative total we have already accounted for
        lot.on_fill(rung=rung, filled_qty=filled, fill_id=fill_identity(update))
        self._remember(lot)

    # ── the timer ───────────────────────────────────────────────────────────

    def heartbeat(self) -> None:
        """Print every open lot's state even when nothing changes.

        A process with nothing to do and one that has silently stopped working
        look identical in a quiet log, and we could not tell them apart for two
        days. Event-driven logging cannot fix that by construction: no events,
        no lines.
        """
        if not self.lots:
            logger.info("heartbeat: no open lots")
            return
        for lot in self.lots.values():
            gaps = ""
            if lot.last_price is not None and lot.stage == "ladder":
                gaps = " ".join(
                    f"tp{r}{lot.target_price(r) - lot.last_price:+.2f}"
                    for r in range(1, len(lot.plan.tranches) + 1)
                    if r not in lot.filled_rungs
                )
            logger.info("heartbeat: lot %s %s stage=%s remaining=%s working_stop=%s "
                        "reserved=%s filled=%s last_price=%s %s",
                        lot.event_id, lot.symbol, lot.stage,
                        assets.format_qty(lot.remaining_qty), lot.working_stop,
                        assets.format_qty(lot.reserved_qty), sorted(lot.filled_rungs),
                        lot.last_price, gaps)

    def reconcile_all(self) -> None:
        """Re-check every open lot against the broker.

        Alpaca does not replay trade_updates missed while the socket was down,
        so a stream that dropped and came back leaves state that looks fine and
        is not. A timer costs one REST call per lot; being wrong costs a
        position.
        """
        for lot in list(self.lots.values()):
            try:
                self._remember(reconcile_lot(lot, self.broker))
            except Exception:
                logger.exception("reconcile failed for lot %s", lot.event_id)
