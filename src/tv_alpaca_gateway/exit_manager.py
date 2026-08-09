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

from dataclasses import dataclass, field, replace
from decimal import ROUND_DOWN, Decimal

from . import assets

# Crypto has no plain stop order, only stop_limit, so a gap straight through the
# limit leaves the position held with its protection unfilled. Wei's number:
# 0.05% below the trigger. Tighter fills better and misses more often.
STOP_LIMIT_OFFSET = Decimal("0.0005")


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

    def validate(self) -> None:
        if not self.tranches:
            raise ExitPlanError("a plan needs at least one take-profit rung")
        total = sum(f for f, _ in self.tranches) + self.runner_fraction
        if total != Decimal("1"):
            raise ExitPlanError(
                f"tranche fractions sum to {total}, not 1 — the position would be "
                "left partly unmanaged")
        if self.trail_source != "previous_completed_bar_low":
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
    stage: str = "ladder"
    stop_order_id: str | None = None
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
            raise ExitPlanError(
                f"stop {self.initial_stop} is not below entry {self.entry_price}; "
                "R would be zero or negative")

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
        return f"pine-exec-{self.event_id}{suffix}"

    @property
    def stop_client_order_id(self) -> str:
        return f"pine-exec-{self.event_id}-protection"

    @property
    def is_closed(self) -> bool:
        return self.stage == "closed" or self.remaining_qty <= 0

    def rung_filled(self, rung: int) -> bool:
        return rung in self.filled_rungs

    # ── price and bar input ─────────────────────────────────────────────────

    def on_price(self, price: Decimal) -> None:
        """A trade print. Fires any breached rung, then checks the working stop."""
        if self.is_closed:
            return
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
        if self.is_closed or self.stage != "runner":
            return
        if trade_count == 0:
            return
        if low > self.working_stop:
            self.working_stop = low          # monotonic: never loosens

    def advance_to_runner(self) -> None:
        """Enter the trailing stage. Used by reconciliation when the ladder is
        already complete, and by tests. Moves no quantity."""
        self.stage = "runner"
        if self.plan.breakeven_after and self.working_stop < self.entry_price:
            self.working_stop = self.entry_price

    def on_fill(self, rung: int, filled_qty: Decimal) -> None:
        """A fill against a rung, whole or partial.

        "I want the whole tranche managed" — so a rung is not done when the
        first fill lands, it is done when the fills add up to the tranche. A
        market order for 3 QQQ may come back as 1 and then 2; treating the
        first as completion strands the rest outside the ladder, with the stop
        sized as though it had all sold.
        """
        if rung in self.filled_rungs:
            return
        self.rung_filled_qty[rung] = self.rung_filled_qty.get(rung, Decimal("0")) + filled_qty
        self.remaining_qty -= filled_qty
        self.pending_rungs.discard(rung)          # let the remainder re-fire

        if self.rung_filled_qty[rung] < self.tranche_qty(rung):
            return                                 # still working; stop stays put

        self.filled_rungs.add(rung)
        if rung == self.plan.breakeven_after and self.working_stop < self.entry_price:
            self.working_stop = self.entry_price
        if len(self.filled_rungs) >= len(self.plan.tranches):
            self.stage = "runner"

        # An oversized sell stop is rejected at the moment it triggers — the
        # failure surfaces exactly when the protection is needed.
        self._resize_stop()

    # ── broker effects ──────────────────────────────────────────────────────

    def _sellable(self, wanted: Decimal) -> Decimal:
        """Independent lots are our fiction; the broker has one position per
        symbol. Clamping to what is really held turns a silent accounting drift
        into a small visible one instead of an order for coins we do not own."""
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
        self._broker.submit_order(
            symbol=self.symbol, side="sell", qty=assets.format_qty(qty),
            type="market", time_in_force=assets.time_in_force(self.symbol),
            client_order_id=self.rung_client_order_id(rung, attempt))

    def _exit_remainder(self, reason: str) -> None:
        """The working stop was breached, or the runner's trail was hit."""
        self._reserve(Decimal("0"))       # the stop holds the coins we must sell
        qty = self._sellable(self.remaining_qty)
        if qty >= self.min_order_size:
            self._broker.submit_order(
                symbol=self.symbol, side="sell", qty=assets.format_qty(qty),
                type="market", time_in_force=assets.time_in_force(self.symbol),
                client_order_id=f"pine-exec-{self.event_id}-{reason}")
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
                  pending_rungs=set(stored.pending_rungs))
    lot._broker = broker
    position = Decimal(str(broker.position_qty(lot.symbol)))

    for rung in range(1, len(lot.plan.tranches) + 1):
        order = broker.get_order_by_client_id(lot.rung_client_order_id(rung))
        if order and order.get("status") == "filled":
            lot.filled_rungs.add(rung)
            lot.pending_rungs.discard(rung)
            if rung == lot.plan.breakeven_after and lot.working_stop < lot.entry_price:
                lot.working_stop = lot.entry_price

    if len(lot.filled_rungs) >= len(lot.plan.tranches):
        lot.stage = "runner"

    lot.remaining_qty = min(lot.remaining_qty, position)
    if lot.remaining_qty <= 0:
        lot.stage = "closed"
    return lot
