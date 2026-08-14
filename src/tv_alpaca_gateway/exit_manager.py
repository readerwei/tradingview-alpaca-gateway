"""Gateway-side scale-out and trail, in two tiers.

The rules and the reasoning are in tests/test_exit_manager_contract.py. The
short version of the shape:

    broker   ONE resting stop per lot, at the entry's original stop. A disaster
             floor, not the working stop. It is only ever resized smaller after
             a partial fill, and never widened.
    gateway  breakeven and the bar-low trail, held here as numbers, fired as
             market sells when breached.

Everything in this module is a decision, not an I/O call. The broker is
injected and is only ever asked to submit, cancel, or report — so what the
manager decides can be tested without a network, and a fake cannot quietly
teach it a rule that belongs here.
"""

from __future__ import annotations

import logging

import json
from dataclasses import dataclass, field, replace
from decimal import ROUND_DOWN, Decimal

from . import assets

# Crypto has no plain stop order, only stop_limit, so a gap straight through the
# limit leaves the position held with its protection unfilled. Wei's number:
# 0.05% below the trigger. Tighter fills better and misses more often.
logger = logging.getLogger(__name__)

STOP_LIMIT_OFFSET = Decimal("0.0005")


def _prefixed(event_id: str) -> str:
    """`pine-exec-<id>`, without doubling it.

    `_command_id` already returns `pine-exec-<identity>`, and that value is what
    reaches a lot as its `event_id`. Prefixing again produced live orders like

        pine-exec-pine-exec-btc-direct-20260811-001-protection-0

    which is harmless — every generator and matcher doubled identically, so
    routing and reconciliation agreed — but it is wrong, it doubles the prefix
    in every audit trail, and it eats the 128-character client-order-id budget
    twice as fast as it should.

    Safe to change only because no lot is open. An id format change would
    otherwise leave a resting stop unfindable by the code that placed it.
    """
    return event_id if event_id.startswith("pine-exec-") else f"pine-exec-{event_id}"


class ExitPlanError(ValueError):
    """The plan cannot be executed as written — raised before any order."""


class LotConflict(RuntimeError):
    """A second entry arrived while a lot is still open on this symbol."""


@dataclass(frozen=True)
class ExitPlan:
    """A named ladder. Values, not references — an open lot keeps its own copy,
    so editing the config cannot re-price a position already in the market."""

    name: str
    tranches: tuple[tuple[Decimal, Decimal], ...]   # (fraction, r_multiple)
    runner_fraction: Decimal
    trail_source: str
    breakeven_after: int
    # Whether a completed bar's HIGH can trigger a rung, not just a trade print.
    # Per-plan rather than global: it changes when a strategy takes profit, so
    # it belongs with the tranches and the R-multiples, not in the engine.
    rungs_on_bar_high: bool = False

    def validate(self) -> None:
        if not self.tranches:
            raise ExitPlanError("a plan needs at least one take-profit rung")
        total = sum(f for f, _ in self.tranches) + self.runner_fraction
        if total != Decimal("1"):
            raise ExitPlanError(
                f"tranche fractions sum to {total}, not 1 — the position would be "
                "left partly unmanaged")
        if self.runner_fraction == 0:
            # No runner means nothing to trail. Demanding a trail source anyway
            # would force every take-profit-and-stop plan to name a mechanism it
            # never uses.
            if self.trail_source not in ("none", "previous_completed_bar_low"):
                raise ExitPlanError(f"unsupported trail source: {self.trail_source!r}")
        elif self.trail_source != "previous_completed_bar_low":
            raise ExitPlanError(f"unsupported trail source: {self.trail_source!r}")


@dataclass
class Lot:
    """One entry and the exits that belong to it.

    Quantities are Decimal throughout. The smallest tranche of a BTC entry is
    ~0.0003; there is no integer anywhere in this lifecycle.
    """

    event_id: str
    symbol: str
    entry_price: Decimal
    initial_stop: Decimal
    held_qty: Decimal
    timeframe: str
    plan: ExitPlan
    min_order_size: Decimal

    remaining_qty: Decimal = Decimal("0")
    working_stop: Decimal = Decimal("0")
    filled_rungs: set[int] = field(default_factory=set)
    pending_rungs: set[int] = field(default_factory=set)
    rung_filled_qty: dict[int, Decimal] = field(default_factory=dict)
    rung_attempts: dict[int, int] = field(default_factory=dict)
    rung_order_ids: dict[int, list[str]] = field(default_factory=dict)
    seen_fills: set[str] = field(default_factory=set)
    stage: str = "ladder"
    stop_order_id: str | None = None
    # Prices supplied by the alert, one per rung, instead of derived from R.
    # Wei: "I will provide explicit stop and take profit prices on the OCO
    # plan." An absolute price cannot go stale between the alert firing and
    # the fill the way an R-multiple cannot go wrong — but it is what the
    # strategy actually computed, and for a single-target plan there is no
    # ladder geometry for R to express anyway.
    explicit_targets: tuple[Decimal, ...] = ()
    # Breakeven that could not be applied yet, and the last price seen.
    breakeven_pending: bool = False
    last_price: Decimal | None = None
    reserved_qty: Decimal = Decimal("0")
    stop_generation: int = 0
    _broker: object | None = field(default=None, repr=False, compare=False)

    # ── construction ────────────────────────────────────────────────────────

    @classmethod
    def opened(cls, **fields) -> "Lot":
        lot = cls(**fields)
        lot.remaining_qty = lot.held_qty
        lot.working_stop = lot.initial_stop
        lot._validate()
        return lot

    def _validate(self) -> None:
        if not self.timeframe:
            raise ExitPlanError(
                "no timeframe on the lot — 'previous completed bar low' has no "
                "meaning without a bar size; take it from the alert's interval")
        self.plan.validate()
        if self.held_qty <= 0:
            raise ExitPlanError("a lot needs a positive held quantity")
        if self.initial_stop >= self.entry_price:
            # Long-only, said out loud. Everything here assumes it: targets sit
            # above entry, rungs fire on price >= target, the trail ratchets up
            # on bar LOWS, and every exit is a sell. A short entry satisfies
            # none of that, and the giveaway is a stop above the entry.
            #
            # Without this the failure was still safe but misleading — it came
            # out as "R would be zero or negative", which reads like a bad
            # alert rather than an unsupported direction, and would have sent
            # whoever hit it looking in the wrong place.
            raise ExitPlanError(
                f"stop {self.initial_stop} is at or above entry {self.entry_price}. "
                "The exit manager is long-only: targets are above entry, the "
                "trail follows bar lows, and every exit is a sell. A short "
                "position needs its own plan, not this one inverted")

        # Every rung is checked now, not when it fires. A ladder that clears TP1
        # and then cannot place TP2 leaves a position half managed, with a
        # remainder the manager retries and fails to sell forever.
        remaining = self.held_qty
        for rung in range(1, len(self.plan.tranches) + 1):
            qty = self.tranche_qty(rung)
            if qty < self.min_order_size:
                raise ExitPlanError(
                    f"rung {rung} is {qty}, below the {self.min_order_size} "
                    f"min_order_size for {self.symbol}")
            remaining -= qty
        if remaining != 0 and remaining < self.min_order_size:
            raise ExitPlanError(
                f"the runner would be {remaining}, below the "
                f"{self.min_order_size} min_order_size — unsellable dust")

    # ── the ladder, priced from the fill ────────────────────────────────────

    @property
    def risk_per_unit(self) -> Decimal:
        """R. Measured from the price the entry actually filled at, which a
        market order into a fast tape will not make equal to the signal price."""
        return self.entry_price - self.initial_stop

    def target_price(self, rung: int) -> Decimal:
        if rung <= len(self.explicit_targets):
            return self.explicit_targets[rung - 1]
        _fraction, multiple = self.plan.tranches[rung - 1]
        return self.entry_price + multiple * self.risk_per_unit

    def _increment(self) -> Decimal:
        """Quantity granularity, taken from the asset's own minimum: 1e-9 for
        BTC, 1 for a share."""
        return Decimal(1).scaleb(self.min_order_size.as_tuple().exponent)

    def tranche_qty(self, rung: int) -> Decimal:
        fraction, _multiple = self.plan.tranches[rung - 1]
        return (self.held_qty * fraction).quantize(self._increment(), ROUND_DOWN)

    def runner_qty(self) -> Decimal:
        """The remainder, deliberately. 20/30/50 of an odd quantity does not
        divide cleanly; rounding each leg independently either strands dust or
        oversells into a quantity the account does not hold."""
        return self.held_qty - sum(self.tranche_qty(r)
                                   for r in range(1, len(self.plan.tranches) + 1))

    def rung_client_order_id(self, rung: int, attempt: int = 0) -> str:
        """Deterministic, so a double-fire is impossible rather than unlikely.

        A crash between deciding to sell and hearing back leaves no local
        record. On restart the target still looks breached — and Alpaca rejects
        the duplicate id, so the retry is refused at the broker.

        `attempt` exists only for a rung that filled partially and is being
        topped up: that second order is genuinely a new order, and must not
        collide with the first. Attempt 0 keeps the plain id so the common case
        stays readable in the order log.
        """
        suffix = f"-tp{rung}" if not attempt else f"-tp{rung}r{attempt}"
        return f"{_prefixed(self.event_id)}{suffix}"

    @property
    def stop_client_order_id(self) -> str:
        return f"{_prefixed(self.event_id)}-protection"

    @property
    def is_closed(self) -> bool:
        return self.stage == "closed" or self.remaining_qty <= 0

    def rung_filled(self, rung: int) -> bool:
        return rung in self.filled_rungs

    # ── price and bar input ─────────────────────────────────────────────────

    def describe(self) -> str:
        """One line of everything this lot currently believes.

        Written because the opposite cost three days. The system was silent
        about its DECISIONS, not just its data — six live runs armed correctly
        and did nothing, and no log said "TP1 is 63.74 away" or "the trail did
        not move because that bar had no trades". Received-message logging
        would not have shown any of it; only the reasoning does.
        """
        rungs = ",".join(
            f"tp{r}{'=filled' if r in self.filled_rungs else ('=pending' if r in self.pending_rungs else f'@{self.target_price(r):.2f}')}"
            for r in range(1, len(self.plan.tranches) + 1))
        return (f"lot {self.event_id} {self.symbol} stage={self.stage} "
                f"remaining={assets.format_qty(self.remaining_qty)} "
                f"entry={self.entry_price} stop={self.initial_stop} "
                f"working_stop={self.working_stop}"
                f"{' breakeven_pending' if self.breakeven_pending else ''} "
                f"reserved={assets.format_qty(self.reserved_qty)} {rungs}")

    def on_price(self, price: Decimal) -> None:
        """A trade print. Fires any breached rung, then checks the working stop."""
        if self.is_closed:
            return
        self.last_price = price
        if self.breakeven_pending and price > self.entry_price:
            logger.info("breakeven now reachable on %s: stop -> %s",
                        self.symbol, self.entry_price)
            self.working_stop = max(self.working_stop, self.entry_price)
            self.breakeven_pending = False
        # Logged after the update so the line reflects what the lot believes
        # having seen this price, not what it believed before.
        if logger.isEnabledFor(logging.DEBUG):
            gaps = " ".join(
                f"tp{r} {self.target_price(r) - price:+.2f}"
                for r in range(1, len(self.plan.tranches) + 1)
                if r not in self.filled_rungs)
            logger.debug("price %s %s | %s | stop %+.2f | %s", self.symbol, price,
                         gaps or "no rungs left",
                         self.working_stop - price, self.describe())
        if self.stage == "ladder":
            for rung in range(1, len(self.plan.tranches) + 1):
                if rung in self.filled_rungs or rung in self.pending_rungs:
                    continue
                if price >= self.target_price(rung):
                    self._fire_rung(rung)
        # Only act when the software stop is strictly tighter than the resting
        # one. While the two sit at the same price a breach triggers both, and
        # the broker's stop is already there and does not depend on us being
        # alive — so selling here as well would just race our own order.
        if self.working_stop > self.initial_stop and price <= self.working_stop:
            self._exit_remainder("stop")

    def on_bar(self, high: Decimal, low: Decimal, close: Decimal,
               trade_count: int | None = None) -> None:
        """A COMPLETED bar. The forming candle is deliberately not an input:
        a stop that follows it ratchets up on every tick and exits on noise the
        bar itself would have closed above.

        Bars with no trades are ignored. Over twelve hours of BTC/USD, two
        thirds of Alpaca's 1m bars had a trade count of zero — they are built
        from quotes, and many have low == high. Trailing off those walks the
        stop up to prices nothing ever traded at, and the next spread wobble
        takes the position out.
        """
        if self.is_closed:
            return
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("bar %s h=%s l=%s c=%s trades=%s | stage=%s working_stop=%s",
                         self.symbol, high, low, close, trade_count,
                         self.stage, self.working_stop)
        if trade_count == 0:
            logger.debug("bar ignored: no trades in it, so its low is a quote "
                         "and not a price anything changed hands at")
            # No trades: the bar is built from quotes, and neither a stop nor a
            # take-profit should act on a price nothing traded at.
            return

        # A target crossed by the bar's HIGH was genuinely reached, whether or
        # not a trade printed there in the instant we were listening.
        #
        # Alpaca's crypto feed made this necessary rather than merely nice:
        # measured over twelve hours, only 34% of BTC/USD 1m bars contain any
        # trade at all. A live example — TP1 at 65,139.84, and the minute that
        # crossed it looked like this:
        #
        #     05:59  h=65,095.98  trades=0
        #     06:00  h=65,153.21  trades=1   <- the whole breach, one print
        #     06:01  h=65,116.90  trades=0
        #
        # Firing only on trade prints gives the ladder one chance per target,
        # and sometimes none. Checking the bar high turns "we must catch the
        # tick" into "we cannot miss the minute", at a cost of up to one bar of
        # latency on a strategy whose runner already trails on bar closes.
        if self.stage == "ladder" and self.plan.rungs_on_bar_high:
            for rung in range(1, len(self.plan.tranches) + 1):
                if rung in self.filled_rungs or rung in self.pending_rungs:
                    continue
                if high >= self.target_price(rung):
                    self._fire_rung(rung)

        if self.stage != "runner":
            return
        if low > self.working_stop:
            logger.info("trail %s: stop %s -> %s (bar low, %s trades)",
                        self.symbol, self.working_stop, low, trade_count)
            self.working_stop = low          # monotonic: never loosens
        elif self.stage == "runner":
            logger.debug("trail unchanged: bar low %s is not above the stop %s",
                         low, self.working_stop)

    def _apply_breakeven(self) -> None:
        """Move the working stop to entry — but never above the market.

        Breakeven is a protective improvement, not an exit instruction. Setting
        a stop above the current price is a market exit wearing a stop's name,
        and on 2026-08-11 that is exactly what happened: a take-profit
        deliberately placed below entry (to make a rung fire on demand) filled,
        breakeven moved the stop to entry, entry was already above the market,
        and the runner was closed 117 seconds later without ever trailing.

        The behaviour was correct for the inputs and startling anyway. So when
        entry is not yet reachable the move is DEFERRED rather than refused:
        the original disaster stop keeps protecting, and breakeven applies the
        moment price trades back above cost.

        Deferring rather than rejecting the configuration matters — an explicit
        target below entry is the only way to make a rung fire on demand, and
        that technique is what finally proved this system works after six runs
        that proved nothing.
        """
        if self.last_price is not None and self.last_price <= self.entry_price:
            self.breakeven_pending = True
            return
        self.working_stop = self.entry_price
        self.breakeven_pending = False

    def advance_to_runner(self) -> None:
        """Enter the trailing stage. Used by reconciliation when the ladder is
        already complete, and by tests. Moves no quantity."""
        self.stage = "runner"
        if self.plan.breakeven_after and self.working_stop < self.entry_price:
            self.working_stop = self.entry_price

    def on_fill(self, rung: int, filled_qty: Decimal, fill_id: str | None = None) -> None:
        """A fill against a rung, whole or partial.

        "I want the whole tranche managed" — so a rung is not done when the
        first fill lands, it is done when the fills add up to the tranche. A
        market order for 3 QQQ may come back as 1 and then 2; treating the
        first as completion strands the rest outside the ladder, with the stop
        sized as though it had all sold.

        `fill_id` deduplicates. Alpaca's trade_updates stream can redeliver,
        and a redelivered partial would otherwise be counted twice — the lot
        would believe it holds less than it does and under-size its own
        protection. Idempotency at submission (a deterministic client order id)
        does nothing for a message arriving twice on the way back.
        """
        if fill_id is not None:
            if fill_id in self.seen_fills:
                return
            self.seen_fills.add(fill_id)
        if rung in self.filled_rungs:
            return
        self.rung_filled_qty[rung] = self.rung_filled_qty.get(rung, Decimal("0")) + filled_qty
        self.remaining_qty -= filled_qty
        self.pending_rungs.discard(rung)          # let the remainder re-fire

        if self.rung_filled_qty[rung] < self.tranche_qty(rung):
            return                                 # still working; stop stays put

        self.filled_rungs.add(rung)
        if rung == self.plan.breakeven_after and self.working_stop < self.entry_price:
            self._apply_breakeven()
        if len(self.filled_rungs) >= len(self.plan.tranches):
            self.stage = "runner"

        # An oversized sell stop is rejected at the moment it triggers — the
        # failure surfaces exactly when the protection is needed.
        self._resize_stop()

    # ── broker effects ──────────────────────────────────────────────────────

    def _require_broker(self) -> None:
        """A lot that has not been through `open_lot` has no broker and no
        resting stop. Saying so beats an AttributeError from three frames
        down, which is what it used to give."""
        if self._broker is None:
            raise ExitPlanError(
                f"lot {self.event_id} is not armed — open_lot() was never called, "
                "so it has no broker and no resting stop")

    def _sellable(self, wanted: Decimal) -> Decimal:
        """Independent lots are our fiction; the broker has one position per
        symbol. Clamping to what is really held turns a silent accounting drift
        into a small visible one instead of an order for coins we do not own."""
        self._require_broker()
        held = Decimal(str(self._broker.position_qty(self.symbol)))
        return min(wanted, held, self.remaining_qty)

    def _fire_rung(self, rung: int) -> None:
        """Free the tranche from under the stop, then sell it.

        The order matters. A resting stop reserves its quantity, so with the
        stop covering the whole position `qty_available` is zero and a
        take-profit sell is rejected outright — observed on the live account:

            qty            0.00149625
            qty_available  0

        Cancelling first and re-placing after the sell would leave the whole
        position naked across two round-trips. Cancelling, re-placing at the
        size the lot will have *once the rung fills*, and only then selling
        leaves a single short gap, and leaves the stop already correctly sized
        if the sell never lands.
        """
        outstanding = self.tranche_qty(rung) - self.rung_filled_qty.get(rung, Decimal("0"))
        qty = self._sellable(outstanding)
        if qty < self.min_order_size:
            return
        self._reserve(self._sellable(self.remaining_qty) - qty)
        self.pending_rungs.add(rung)
        attempt = self.rung_attempts.get(rung, 0)
        self.rung_attempts[rung] = attempt + 1
        placed = self._broker.submit_order(
            symbol=self.symbol, side="sell", qty=assets.format_qty(qty),
            type="market", time_in_force=assets.time_in_force(self.symbol),
            client_order_id=self.rung_client_order_id(rung, attempt))
        self.rung_order_ids.setdefault(rung, []).append(placed["id"])

    def _exit_remainder(self, reason: str) -> None:
        """The working stop was breached, or the runner's trail was hit.

        Any rung still in flight is cancelled first. A take-profit is a market
        order and does not rest for long, but "not for long" is not "never":
        one filling alongside the remainder exit sells the same coins twice —
        rejected on crypto, and an unintended short on anything that can go
        short.
        """
        for rung in sorted(self.pending_rungs):
            for order_id in self.rung_order_ids.get(rung, []):
                self._broker.cancel_order(order_id)
        self.pending_rungs.clear()
        self._reserve(Decimal("0"))       # the stop holds the coins we must sell
        qty = self._sellable(self.remaining_qty)
        if qty >= self.min_order_size:
            self._broker.submit_order(
                symbol=self.symbol, side="sell", qty=assets.format_qty(qty),
                type="market", time_in_force=assets.time_in_force(self.symbol),
                client_order_id=f"{_prefixed(self.event_id)}-{reason}")
        self.stage = "closed"
        self.remaining_qty = Decimal("0")

    def _resize_stop(self) -> None:
        self._reserve(self._sellable(self.remaining_qty))

    def _reserve(self, qty: Decimal) -> None:
        """Make the resting disaster stop cover exactly `qty`.

        Cancel then place — Wei's call, and on crypto the only option: with the
        old stop still resting there is no available quantity for a replacement
        to reserve, however small. A failed placement leaves the position naked,
        which is why the caller retries and then flattens.
        """
        if qty == self.reserved_qty and self.stop_order_id:
            return
        if self.stop_order_id:
            self._broker.cancel_order(self.stop_order_id)
            self.stop_order_id = None
            self.reserved_qty = Decimal("0")
        if qty < self.min_order_size:
            return
        self.stop_order_id = self._place_stop(qty)
        self.reserved_qty = qty

    def _place_stop(self, qty: Decimal) -> str:
        order = build_stop_order(self.symbol, qty, self.initial_stop)
        order["client_order_id"] = f"{self.stop_client_order_id}-{self.stop_generation}"
        self.stop_generation += 1
        return self._broker.submit_order(**order)["id"]


_DECIMAL_FIELDS = ("entry_price", "initial_stop", "held_qty", "min_order_size",
                   "remaining_qty", "working_stop", "reserved_qty")


def dump_lot(lot: Lot) -> str:
    """Serialise a lot for the store.

    Every quantity and price goes through as a **string**, never a float.
    ``float(Decimal("0.00149625"))`` and back is not the same number, and this
    is a system where a quantity that is off in the ninth place is an order the
    broker rejects. JSON's native number type is exactly the wrong tool here.
    """
    state = {
        "event_id": lot.event_id, "symbol": lot.symbol, "timeframe": lot.timeframe,
        "stage": lot.stage, "stop_order_id": lot.stop_order_id,
        "stop_generation": lot.stop_generation,
        "explicit_targets": [str(t) for t in lot.explicit_targets],
        "breakeven_pending": lot.breakeven_pending,
        "last_price": str(lot.last_price) if lot.last_price is not None else None,
        "filled_rungs": sorted(lot.filled_rungs),
        "pending_rungs": sorted(lot.pending_rungs),
        "rung_filled_qty": {str(k): str(v) for k, v in lot.rung_filled_qty.items()},
        "rung_attempts": {str(k): v for k, v in lot.rung_attempts.items()},
        "rung_order_ids": {str(k): list(v) for k, v in lot.rung_order_ids.items()},
        "seen_fills": sorted(lot.seen_fills),
        "plan": {
            "name": lot.plan.name,
            "tranches": [[str(f), str(m)] for f, m in lot.plan.tranches],
            "runner_fraction": str(lot.plan.runner_fraction),
            "trail_source": lot.plan.trail_source,
            "breakeven_after": lot.plan.breakeven_after,
        },
    }
    state |= {name: str(getattr(lot, name)) for name in _DECIMAL_FIELDS}
    return json.dumps(state)


def load_lot(state: str) -> Lot:
    """Rebuild a lot from the store. Not validated on the way in: a lot that is
    already open must be recoverable even if a config change would now make its
    plan illegal, or a restart would abandon a live position."""
    raw = json.loads(state)
    plan = raw["plan"]
    lot = Lot(
        event_id=raw["event_id"], symbol=raw["symbol"], timeframe=raw["timeframe"],
        plan=ExitPlan(
            name=plan["name"],
            tranches=tuple((Decimal(f), Decimal(m)) for f, m in plan["tranches"]),
            runner_fraction=Decimal(plan["runner_fraction"]),
            trail_source=plan["trail_source"],
            breakeven_after=plan["breakeven_after"],
        ),
        **{name: Decimal(raw[name]) for name in _DECIMAL_FIELDS},
    )
    lot.explicit_targets = tuple(Decimal(t) for t in raw.get("explicit_targets", []))
    lot.breakeven_pending = bool(raw.get("breakeven_pending", False))
    _last = raw.get("last_price")
    lot.last_price = Decimal(_last) if _last else None
    lot.stage = raw["stage"]
    lot.stop_order_id = raw["stop_order_id"]
    lot.stop_generation = raw["stop_generation"]
    lot.filled_rungs = set(raw["filled_rungs"])
    lot.pending_rungs = set(raw["pending_rungs"])
    lot.rung_filled_qty = {int(k): Decimal(v) for k, v in raw["rung_filled_qty"].items()}
    lot.rung_attempts = {int(k): v for k, v in raw["rung_attempts"].items()}
    lot.rung_order_ids = {int(k): list(v) for k, v in raw.get("rung_order_ids", {}).items()}
    lot.seen_fills = set(raw.get("seen_fills", []))
    return lot


def build_stop_order(symbol: str, qty: Decimal, stop_price: Decimal,
                     trail_percent: Decimal | None = None) -> dict:
    """The disaster stop, in the form the asset class actually accepts.

    ``trail_percent`` exists only to be refused on crypto. Alpaca has no
    trailing_stop order type there, and the runner trails in software precisely
    so this order never has to be built.
    """
    if trail_percent is not None and assets.is_crypto(symbol):
        raise ExitPlanError(
            f"Alpaca does not support a trailing_stop order on {symbol}; crypto "
            "runners trail in software")
    order = {
        "symbol": symbol,
        "side": "sell",
        "qty": assets.format_qty(qty),
        "time_in_force": assets.time_in_force(symbol),
    }
    if trail_percent is not None:
        order |= {"type": "trailing_stop", "trail_percent": str(trail_percent)}
    elif assets.is_crypto(symbol):
        limit = (stop_price * (1 - STOP_LIMIT_OFFSET)).quantize(Decimal("0.01"))
        order |= {"type": "stop_limit", "stop_price": str(stop_price),
                  "limit_price": str(limit)}
    else:
        order |= {"type": "stop", "stop_price": str(stop_price)}
    return order


def open_lot(lot: Lot, broker) -> Lot:
    """Arm a lot: refuse if the symbol is busy, then rest the disaster stop."""
    if assets.is_crypto(lot.symbol):
        resting = [o for o in broker.open_orders(lot.symbol) if o.get("side") == "sell"]
        if resting:
            raise LotConflict(
                f"{lot.symbol} already has an open lot with {len(resting)} resting "
                "sell(s); a resting sell also blocks the entry buy, so this is "
                "refused here rather than by Alpaca with no order record")
    lot._broker = broker
    lot._reserve(lot._sellable(lot.remaining_qty))
    return lot


def reconcile_lot(stored: Lot, broker) -> Lot:
    """Rebuild from the account, which is the fact; the database is a cache.

    Runtime state has diverged from what we believed was running four times
    this week. A lot recorded as 80% open against a flat account comes back
    closed rather than re-arming a ladder against coins that are gone.
    """
    # set() copies, not shared references: `replace` passes the stored lot's own
    # sets straight through, so mutating the rebuilt lot would edit the record
    # it was rebuilt from.
    lot = replace(stored, filled_rungs=set(stored.filled_rungs),
                  pending_rungs=set(stored.pending_rungs),
                  rung_filled_qty=dict(stored.rung_filled_qty),
                  rung_attempts=dict(stored.rung_attempts),
                  rung_order_ids={k: list(v) for k, v in stored.rung_order_ids.items()},
                  seen_fills=set(stored.seen_fills))
    lot._broker = broker
    position = Decimal(str(broker.position_qty(lot.symbol)))

    # Every attempt, not just the first. A rung that filled partially and was
    # topped up has a second order under a different client id; looking only at
    # the base id reconstructs it as unfilled and sells the tranche again.
    for rung in range(1, len(lot.plan.tranches) + 1):
        total = Decimal("0")
        for attempt in range(lot.rung_attempts.get(rung, 0) + 1):
            order = broker.get_order_by_client_id(lot.rung_client_order_id(rung, attempt))
            if order:
                total += Decimal(str(order.get("filled_qty") or "0"))
        if total > lot.rung_filled_qty.get(rung, Decimal("0")):
            lot.rung_filled_qty[rung] = total
        if lot.rung_filled_qty.get(rung, Decimal("0")) >= lot.tranche_qty(rung):
            lot.filled_rungs.add(rung)
            lot.pending_rungs.discard(rung)
            if rung == lot.plan.breakeven_after and lot.working_stop < lot.entry_price:
                lot.working_stop = lot.entry_price

    if len(lot.filled_rungs) >= len(lot.plan.tranches):
        lot.stage = "runner"

    lot.remaining_qty = min(lot.remaining_qty, position)
    if lot.remaining_qty <= 0:
        lot.stage = "closed"
        lot.stop_order_id, lot.reserved_qty = None, Decimal("0")
        return lot

    # The resting stop comes from the broker too. Carrying stop_order_id over
    # from the database means a stop that was cancelled or filled while we were
    # down comes back as still resting — and _reserve() short-circuits when it
    # believes the reservation already matches, so the lot would sit unprotected
    # and never notice. This is the failure the whole restart path exists for.
    resting = [o for o in broker.open_orders(lot.symbol)
               if str(o.get("client_order_id") or "").startswith(
                   f"{lot.stop_client_order_id}-")]
    if resting:
        lot.stop_order_id = resting[-1].get("id")
        lot.reserved_qty = Decimal(str(resting[-1]["qty"]))
    else:
        lot.stop_order_id, lot.reserved_qty = None, Decimal("0")

    lot._resize_stop()      # re-protect if the stop is missing or mis-sized
    return lot
