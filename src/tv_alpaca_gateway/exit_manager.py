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
from .market_log import logger as market_logger

# Crypto has no plain stop order, only stop_limit, so a gap straight through the
# limit leaves the position held with its protection unfilled. Wei's number:
# 0.05% below the trigger. Tighter fills better and misses more often.
logger = logging.getLogger(__name__)

STOP_LIMIT_OFFSET = Decimal("0.0005")

# Every client order id this gateway places opens with this, because they are
# all `event_id + suffix` and `execution._command_id` builds the event id from
# it. That single fact is what `is_ours` rests on.
NAMESPACE = "pine-exec-"


def is_ours(client_order_id: str | None) -> bool:
    """Did this gateway place the order?

    Deliberately a namespace test rather than a grammar of known suffixes.
    Both would answer today's question; only this one keeps answering it. A
    suffix table would have to list `-tp{n}`, `-tp{n}r{k}`, `-protection-{gen}`,
    `-stop`, `-oco`, `-flatten`, and would start reporting the gateway's own
    orders as foreign the day someone adds an exit reason and forgets it — a
    false alarm in the one place a false alarm is most expensive.

    The distinction that matters is ours-versus-not, and the account is shared:
    Wei runs a second system against it, so anything outside this namespace is
    genuinely someone else's order and worth a line.
    """
    return (client_order_id or "").startswith(NAMESPACE)


def prefixed(event_id: str) -> str:
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
    return event_id if is_ours(event_id) else f"{NAMESPACE}{event_id}"


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
    # "ladder"  rungs fire when price REACHES a level given in advance.
    # "swing"   a slice is armed by market structure and sold when price FALLS
    #           BACK THROUGH a level the market chose. See _swing_on_bar.
    #
    # A separate style rather than a plan that happens to configure differently:
    # the two answer different questions of a bar, and folding them together
    # would put an `if` inside every decision instead of one at the top.
    exit_style: str = "ladder"
    # SWING ONLY. All inert while exit_style is "ladder".
    swing_arm_count: int = 0            # N cumulatively higher lows to arm
    swing_weaken_count: int = 0         # M consecutive lower lows is weakness
    swing_min_arm_r: Decimal = Decimal("0")     # arm only beyond entry ± this×R
    swing_weak_trail_r: Decimal = Decimal("0")  # the tight trail weakness adopts

    def validate(self) -> None:
        if self.exit_style not in ("ladder", "swing"):
            raise ExitPlanError(f"unknown exit style: {self.exit_style!r}")
        if self.exit_style == "swing":
            # Checked here rather than trusted from the config table, because a
            # swing plan missing its counts would not fail — it would simply
            # never arm, and a plan that silently never takes profit is the
            # worst failure this file can produce.
            if self.swing_arm_count < 1:
                raise ExitPlanError(
                    "a swing plan needs swing_arm_count >= 1; with 0 no slice "
                    "would ever arm and the position would ride to its stop")
            if self.swing_weaken_count < 1:
                raise ExitPlanError(
                    "a swing plan needs swing_weaken_count >= 1; with 0 there "
                    "is no definition of weakness and the tight trail is dead")
            if self.swing_weak_trail_r <= 0:
                raise ExitPlanError(
                    "a swing plan needs a positive swing_weak_trail_r — that is "
                    "the trail every remaining share collapses onto")
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
    # +1 long, -1 short. One sign threading through every decision, rather
    # than `if short` at nine call sites — the same code then runs both ways,
    # so a mistake shows up in both directions instead of hiding in the one
    # nobody exercised.
    direction: int = 1

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
    # ── swing state (SMART_PROFIT); untouched by every other plan ───────────
    # The highest low seen since counting began, and how many bars have beaten
    # it. Wei: a lower low "just does not increment it" — the reference holds
    # and the count holds, so a dip inside a climb costs a bar, not a sequence.
    swing_reference_low: Decimal | None = None
    swing_count: int = 0
    # Which slice is currently trailing, and where each slice's trail sits.
    # Only ever one armed at a time: counting belongs to whichever slice is
    # next, so a slice cannot accumulate structure while its predecessor runs.
    armed_rung: int | None = None
    tranche_trail: dict[int, Decimal] = field(default_factory=dict)
    # Weakness: consecutive lower lows, and the extreme the tight trail hangs
    # from once it triggers.
    prev_bar_low: Decimal | None = None
    weak_count: int = 0
    weakened: bool = False
    high_water: Decimal | None = None
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
        if self.sign * (self.entry_price - self.initial_stop) <= 0:
            # Long-only, said out loud. Everything here assumes it: targets sit
            # above entry, rungs fire on price >= target, the trail ratchets up
            # on bar LOWS, and every exit is a sell. A short entry satisfies
            # none of that, and the giveaway is a stop above the entry.
            #
            # Without this the failure was still safe but misleading — it came
            # out as "R would be zero or negative", which reads like a bad
            # alert rather than an unsupported direction, and would have sent
            # whoever hit it looking in the wrong place.
            where = "at or below" if self.is_short else "at or above"
            raise ExitPlanError(
                f"stop {self.initial_stop} is {where} entry {self.entry_price} "
                f"on a {'short' if self.is_short else 'long'}; R would be zero "
                f"or negative and every target would sit the wrong side of the "
                f"entry")

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
    def sign(self) -> Decimal:
        return Decimal(self.direction)

    @property
    def is_short(self) -> bool:
        return self.direction < 0

    @property
    def exit_side(self) -> str:
        """A long exits by selling; a short exits by buying."""
        return "sell" if self.direction > 0 else "buy"

    @property
    def risk_per_unit(self) -> Decimal:
        """R, always positive in both directions.

        Long: entry above stop. Short: stop above entry. The sign makes it one
        expression rather than a branch, which matters because every target and
        every breach test is derived from it.
        """
        return self.sign * (self.entry_price - self.initial_stop)

    def target_price(self, rung: int) -> Decimal:
        """Above entry for a long, below it for a short."""
        if rung <= len(self.explicit_targets):
            return self.explicit_targets[rung - 1]
        _fraction, multiple = self.plan.tranches[rung - 1]
        return self.entry_price + self.sign * multiple * self.risk_per_unit

    def _reached(self, price: Decimal, level: Decimal) -> bool:
        """Has price got to `level` in the direction the trade profits?"""
        return self.sign * (price - level) >= 0

    def _breached(self, price: Decimal, stop: Decimal) -> bool:
        """Has price gone through the stop, against the trade?"""
        return self.sign * (price - stop) <= 0

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
        return f"{prefixed(self.event_id)}{suffix}"

    @property
    def stop_client_order_id(self) -> str:
        return f"{prefixed(self.event_id)}-protection"

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
        self.last_price = price
        if self.breakeven_pending and self._reached(price, self.entry_price):
            self.working_stop = (min(self.working_stop, self.entry_price) if self.is_short
                                 else max(self.working_stop, self.entry_price))
            self.breakeven_pending = False
        if self.is_swing:
            # One armed slice, sold when price falls back THROUGH its trail —
            # the mirror image of a ladder rung, which fires when price climbs
            # UP TO a level. Nothing else here can sell a slice.
            rung = self.armed_rung
            if (rung is not None and rung not in self.filled_rungs
                    and rung not in self.pending_rungs
                    and self._breached(price, self.tranche_trail[rung])):
                self._fire_rung(rung, level=self.tranche_trail[rung])
        elif self.stage == "ladder":
            for rung in range(1, len(self.plan.tranches) + 1):
                if rung in self.filled_rungs or rung in self.pending_rungs:
                    continue
                if self._reached(price, self.target_price(rung)):
                    self._fire_rung(rung)
        # Only act when the software stop is strictly tighter than the resting
        # one. While the two sit at the same price a breach triggers both, and
        # the broker's stop is already there and does not depend on us being
        # alive — so selling here as well would just race our own order.
        # "strictly tighter" means closer to price in the profitable direction,
        # which is a comparison against the initial stop in the sign's terms.
        if (self.sign * (self.working_stop - self.initial_stop) > 0
                and self._breached(price, self.working_stop)):
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
        if trade_count == 0:
            # Market data, not a decision. On Alpaca's crypto feed 59% of bars
            # are quote-only, so this is the most frequent bar line there is —
            # 50 such bars produced 50 lines on master, which is the flood
            # everything else was moved to stop.
            market_logger.debug(
                "bar %s ignored: no trades, so its low is a quote and not a "
                "price anything changed hands at", self.symbol)
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
        if self.is_swing:
            self._swing_on_bar(high, low)
            return

        if self.stage == "ladder" and self.plan.rungs_on_bar_high:
            for rung in range(1, len(self.plan.tranches) + 1):
                if rung in self.filled_rungs or rung in self.pending_rungs:
                    continue
                # A long needs the bar HIGH to reach a target above it; a
                # short needs the LOW to reach one below.
                extreme = low if self.is_short else high
                if self._reached(extreme, self.target_price(rung)):
                    self._fire_rung(rung)

        if self.stage != "runner":
            return
        # A long trails the bar LOW upward; a short trails the bar HIGH
        # downward. Monotonic in the direction that locks in profit, never the
        # other way.
        candidate = high if self.is_short else low
        if self.sign * (candidate - self.working_stop) > 0:
            logger.info("trail %s: stop %s -> %s (bar %s, %s trades)",
                        self.symbol, self.working_stop, candidate,
                        "high" if self.is_short else "low", trade_count)
            self.working_stop = candidate

    # ── SMART_PROFIT: slices armed by structure ─────────────────────────────

    @property
    def is_swing(self) -> bool:
        return self.plan.exit_style == "swing"

    @property
    def current_rung(self) -> int | None:
        """The slice the market is currently working towards — the first that
        has not been sold. Only this one can count structure or be armed."""
        for rung in range(1, len(self.plan.tranches) + 1):
            if rung not in self.filled_rungs:
                return rung
        return None

    @property
    def arm_gate(self) -> Decimal:
        """No slice arms before the trade is meaningfully in profit.

        Wei: "must be above entry + multiple * R, say multiple = 0.5". Without
        it, three higher lows made entirely below entry would arm a slice and
        sell it for a loss under the name take-profit — the structure rule is
        about trend, and says nothing about whether the trend has paid yet.
        """
        return self.entry_price + self.sign * self.plan.swing_min_arm_r * self.risk_per_unit

    def _swing_on_bar(self, high: Decimal, low: Decimal) -> None:
        """One completed, traded bar through the SMART_PROFIT state machine.

        For a long the structure is higher LOWS and the tight trail hangs from
        the highest HIGH; a short mirrors both through `sign`, so `structure`
        and `extreme` below are the only place the direction is read.
        """
        structure = high if self.is_short else low       # the swing point
        extreme = low if self.is_short else high         # the profit extreme

        # Tracked from the first bar, not from the moment weakness is declared.
        # The peak of a move happens BEFORE it starts weakening — that is what
        # weakening means — so a trail hung only off post-weakness bars starts
        # below where the run actually reached and gives back the difference.
        if self.high_water is None or self.sign * (extreme - self.high_water) > 0:
            self.high_water = extreme

        if self.weakened:
            self._update_weak_trail(extreme)
            return

        # Weakness first: it overrides everything, including an armed slice.
        # Consecutive, unlike the higher-low count — one bar that breaks lower
        # inside a climb is noise, two in a row is the structure failing.
        if self.prev_bar_low is not None and self.sign * (structure - self.prev_bar_low) < 0:
            self.weak_count += 1
        else:
            self.weak_count = 0
        self.prev_bar_low = structure
        if self.weak_count >= self.plan.swing_weaken_count:
            self._enter_weakness(extreme)
            return

        rung = self.current_rung
        if rung is None:
            return

        if self.armed_rung == rung:
            # Follow the structure up. Monotonic: a lower low leaves it alone.
            if self.sign * (structure - self.tranche_trail[rung]) > 0:
                logger.info("lot %s %s: slice %d trail %s -> %s",
                            self.event_id, self.symbol, rung,
                            self.tranche_trail[rung], structure)
                self.tranche_trail[rung] = structure
            return

        # Counting. The first bar establishes what later bars must beat —
        # there is nothing behind it for it to be higher than, so it sets the
        # reference without counting.
        if self.swing_reference_low is None:
            self.swing_reference_low = structure
            return
        # A bar counts only if it beats EVERY low since counting began, not
        # merely the one before it. Failing that it does NOT reset the count,
        # it simply does not add to it — Wei, asked directly: "just not
        # increment it". A dip inside a climb costs a bar, not the sequence.
        if self.sign * (structure - self.swing_reference_low) <= 0:
            return
        self.swing_reference_low = structure
        self.swing_count += 1
        logger.info("lot %s %s: higher %s %d/%d at %s (slice %d)",
                    self.event_id, self.symbol, "high" if self.is_short else "low",
                    self.swing_count, self.plan.swing_arm_count, structure, rung)
        if self.swing_count < self.plan.swing_arm_count:
            return
        if self.sign * (structure - self.arm_gate) < 0:
            # Structure is there and profit is not. Keep counting: the next
            # higher low that clears the gate arms immediately.
            logger.info("lot %s %s: slice %d has its %d %s but %s is short of "
                        "the %s gate; not arming",
                        self.event_id, self.symbol, rung, self.swing_count,
                        "highs" if self.is_short else "lows", structure,
                        self.arm_gate)
            return
        self.armed_rung = rung
        self.tranche_trail[rung] = structure
        logger.info("lot %s %s: slice %d ARMED at %s (%s of the position "
                    "now trails the structure)", self.event_id, self.symbol,
                    rung, structure, self.plan.tranches[rung - 1][0])

    def _enter_weakness(self, extreme: Decimal) -> None:
        """M lower lows. Everything left collapses onto one tight trail.

        Wei: "we will flatten all our remaining positions to trail by 0.1R" —
        tighten and let it stop out, not a market exit. So this hands the whole
        remainder to `working_stop`, which `on_price` already exits on, rather
        than inventing a second exit path that would have to be kept in step.
        """
        logger.info("lot %s %s: WEAKENING — %d consecutive lower %s; every "
                    "remaining share moves onto a %sR trail",
                    self.event_id, self.symbol, self.weak_count,
                    "highs" if self.is_short else "lows",
                    self.plan.swing_weak_trail_r)
        self.weakened = True
        self.armed_rung = None
        self._update_weak_trail(extreme)

    def _update_weak_trail(self, extreme: Decimal) -> None:
        if self.high_water is None or self.sign * (extreme - self.high_water) > 0:
            self.high_water = extreme
        level = self.high_water - self.sign * self.plan.swing_weak_trail_r * self.risk_per_unit
        # Never widens. A 0.1R trail hung off a high water mark barely beyond
        # entry can sit further from price than the disaster stop, and adopting
        # it would answer weakness with MORE risk.
        if self.sign * (level - self.working_stop) > 0:
            logger.info("lot %s %s: weak trail %s -> %s (high water %s)",
                        self.event_id, self.symbol, self.working_stop, level,
                        self.high_water)
            self.working_stop = level

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
        if self.last_price is not None and self._breached(self.last_price, self.entry_price):
            self.breakeven_pending = True
            return
        logger.info("lot %s %s: breakeven — working stop %s -> %s",
                    self.event_id, self.symbol, self.working_stop, self.entry_price)
        self.working_stop = self.entry_price
        self.breakeven_pending = False

    def advance_to_runner(self) -> None:
        """Enter the trailing stage. Used by reconciliation when the ladder is
        already complete, and by tests. Moves no quantity."""
        self.stage = "runner"
        if (self.plan.breakeven_after
                and self.sign * (self.working_stop - self.entry_price) < 0):
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
        logger.info("lot %s %s: rung %d complete (%s filled), %s remaining",
                    self.event_id, self.symbol, rung,
                    assets.format_qty(self.rung_filled_qty[rung]),
                    assets.format_qty(self.remaining_qty))
        if (rung == self.plan.breakeven_after
                and self.sign * (self.working_stop - self.entry_price) < 0):
            self._apply_breakeven()
        if self.is_swing and rung == self.armed_rung:
            # This slice is spent, and so is the structure that armed it. The
            # next needs N FRESH higher lows — Wei's answer to question 4.
            # Carrying the count over would arm the successor almost at once,
            # off highs the market has already paid for.
            self.armed_rung = None
            self.swing_count = 0
            self.swing_reference_low = None
            logger.info("lot %s %s: slice %d taken; counting restarts for "
                        "slice %s", self.event_id, self.symbol, rung,
                        self.current_rung)
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
        # abs(): a short position reports a negative quantity, and an order
        # quantity is always positive. The sign belongs in the price and side
        # logic, never in a number handed to the broker.
        held = abs(Decimal(str(self._broker.position_qty(self.symbol))))
        return min(wanted, held, self.remaining_qty)

    def _fire_rung(self, rung: int, level: Decimal | None = None) -> None:
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
        keeping = self._sellable(self.remaining_qty) - qty
        # `level` is what actually triggered: a swing slice is sold because
        # price fell back through its trail, so logging `target_price` there
        # would print a number nothing consulted.
        logger.info("lot %s %s: rung %d %s %s — freeing %s from the stop "
                    "(reserve %s -> %s), then exiting it",
                    self.event_id, self.symbol, rung,
                    "trail broken at" if level is not None else "reached",
                    level if level is not None else self.target_price(rung),
                    assets.format_qty(qty), assets.format_qty(self.reserved_qty),
                    assets.format_qty(keeping))
        self._reserve(keeping)
        self.pending_rungs.add(rung)
        attempt = self.rung_attempts.get(rung, 0)
        self.rung_attempts[rung] = attempt + 1
        placed = self._broker.submit_order(
            symbol=self.symbol, side=self.exit_side, qty=assets.format_qty(qty),
            type="market", time_in_force=assets.time_in_force(self.symbol),
            client_order_id=self.rung_client_order_id(rung, attempt))
        self.rung_order_ids.setdefault(rung, []).append(placed["id"])
        logger.info("lot %s %s: rung %d %s %s submitted (order %s)",
                    self.event_id, self.symbol, rung, self.exit_side,
                    assets.format_qty(qty), str(placed.get("id"))[:8])

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
        logger.info("lot %s %s: exiting the remainder (%s) — %s; cancelling the "
                    "stop that reserves it",
                    self.event_id, self.symbol,
                    assets.format_qty(self.remaining_qty),
                    "working stop breached" if reason == "stop" else reason)
        self._reserve(Decimal("0"))       # the stop holds the coins we must sell
        qty = self._sellable(self.remaining_qty)
        if qty >= self.min_order_size:
            self._broker.submit_order(
                symbol=self.symbol, side=self.exit_side, qty=assets.format_qty(qty),
                type="market", time_in_force=assets.time_in_force(self.symbol),
                client_order_id=f"{prefixed(self.event_id)}-{reason}")
        logger.info("lot %s %s: closed", self.event_id, self.symbol)
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
        logger.info("lot %s %s: protection %s -> %s at %s",
                    self.event_id, self.symbol,
                    assets.format_qty(self.reserved_qty), assets.format_qty(qty),
                    self.initial_stop)
        if self.stop_order_id:
            self._broker.cancel_order(self.stop_order_id)
            self.stop_order_id = None
            self.reserved_qty = Decimal("0")
        if qty < self.min_order_size:
            return
        self.stop_order_id = self._place_stop(qty)
        self.reserved_qty = qty
        logger.info("lot %s %s: protection resting, %s reserved (order %s)",
                    self.event_id, self.symbol, assets.format_qty(qty),
                    str(self.stop_order_id)[:8])

    def _place_stop(self, qty: Decimal) -> str:
        order = build_stop_order(self.symbol, qty, self.initial_stop,
                                 direction=self.direction)
        order["client_order_id"] = f"{self.stop_client_order_id}-{self.stop_generation}"
        self.stop_generation += 1
        return self._broker.submit_order(**order)["id"]


_DECIMAL_FIELDS = ("entry_price", "initial_stop", "held_qty", "min_order_size",
                   "remaining_qty", "working_stop", "reserved_qty")


def _maybe_decimal(value) -> Decimal | None:
    return None if value is None else Decimal(value)


def _or_none(value: Decimal | None) -> str | None:
    """A Decimal as a string, or None. Never a float — see dump_lot."""
    return None if value is None else str(value)


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
        "direction": lot.direction,
        "explicit_targets": [str(t) for t in lot.explicit_targets],
        "breakeven_pending": lot.breakeven_pending,
        "last_price": str(lot.last_price) if lot.last_price is not None else None,
        "filled_rungs": sorted(lot.filled_rungs),
        "pending_rungs": sorted(lot.pending_rungs),
        "rung_filled_qty": {str(k): str(v) for k, v in lot.rung_filled_qty.items()},
        "rung_attempts": {str(k): v for k, v in lot.rung_attempts.items()},
        "rung_order_ids": {str(k): list(v) for k, v in lot.rung_order_ids.items()},
        "seen_fills": sorted(lot.seen_fills),
        # Every field on ExitPlan, and the round-trip test in
        # test_lot_persistence_contract.py compares the whole dataclass so that
        # stays true. This list was maintained by hand and fell behind:
        # `rungs_on_bar_high` was added to the plan and never added here, so it
        # was written by nobody and read back as the dataclass default. A lot
        # that had been through a restart silently stopped firing rungs on a
        # bar's high — on a feed where only a third of bars carry a trade at
        # all, which is the case the flag was added for.
        "plan": {
            "name": lot.plan.name,
            "tranches": [[str(f), str(m)] for f, m in lot.plan.tranches],
            "runner_fraction": str(lot.plan.runner_fraction),
            "trail_source": lot.plan.trail_source,
            "breakeven_after": lot.plan.breakeven_after,
            "rungs_on_bar_high": lot.plan.rungs_on_bar_high,
            "exit_style": lot.plan.exit_style,
            "swing_arm_count": lot.plan.swing_arm_count,
            "swing_weaken_count": lot.plan.swing_weaken_count,
            "swing_min_arm_r": str(lot.plan.swing_min_arm_r),
            "swing_weak_trail_r": str(lot.plan.swing_weak_trail_r),
        },
        # Swing state. A restart mid-trend must not re-count from zero or
        # forget which slice is trailing and at what level — that would sell
        # the wrong fraction, or none.
        "swing_reference_low": _or_none(lot.swing_reference_low),
        "swing_count": lot.swing_count,
        "armed_rung": lot.armed_rung,
        "tranche_trail": {str(k): str(v) for k, v in lot.tranche_trail.items()},
        "prev_bar_low": _or_none(lot.prev_bar_low),
        "weak_count": lot.weak_count,
        "weakened": lot.weakened,
        "high_water": _or_none(lot.high_water),
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
            # `.get` because rows written before this field was persisted have
            # no such key. They take the dataclass default rather than being
            # re-resolved from the plan name: a lot snapshots its plan at fill
            # time precisely so that editing a plan cannot re-price a position
            # already in the market, and reaching back into the config on load
            # would defeat that from the other direction.
            rungs_on_bar_high=plan.get("rungs_on_bar_high", False),
            exit_style=plan.get("exit_style", "ladder"),
            swing_arm_count=plan.get("swing_arm_count", 0),
            swing_weaken_count=plan.get("swing_weaken_count", 0),
            swing_min_arm_r=Decimal(plan.get("swing_min_arm_r", "0")),
            swing_weak_trail_r=Decimal(plan.get("swing_weak_trail_r", "0")),
        ),
        direction=int(raw.get("direction", 1)),
        **{name: Decimal(raw[name]) for name in _DECIMAL_FIELDS},
    )
    lot.explicit_targets = tuple(Decimal(t) for t in raw.get("explicit_targets", []))
    # `.get` throughout: rows written before SMART_PROFIT existed have none of
    # these, and a ladder lot never reads them.
    lot.swing_reference_low = _maybe_decimal(raw.get("swing_reference_low"))
    lot.swing_count = raw.get("swing_count", 0)
    lot.armed_rung = raw.get("armed_rung")
    lot.tranche_trail = {int(k): Decimal(v)
                         for k, v in raw.get("tranche_trail", {}).items()}
    lot.prev_bar_low = _maybe_decimal(raw.get("prev_bar_low"))
    lot.weak_count = raw.get("weak_count", 0)
    lot.weakened = bool(raw.get("weakened", False))
    lot.high_water = _maybe_decimal(raw.get("high_water"))
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
                     trail_percent: Decimal | None = None,
                     direction: int = 1) -> dict:
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
        # A long is protected by a sell below it; a short by a buy above it.
        "side": "sell" if direction > 0 else "buy",
        "qty": assets.format_qty(qty),
        "time_in_force": assets.time_in_force(symbol),
    }
    if trail_percent is not None:
        order |= {"type": "trailing_stop", "trail_percent": str(trail_percent)}
    elif assets.is_crypto(symbol):
        # The limit sits on the far side of the trigger in the direction the
        # order will execute: below for a sell, above for a buy. Putting it on
        # the wrong side makes an order that can trigger and never fill.
        offset = -STOP_LIMIT_OFFSET if direction > 0 else STOP_LIMIT_OFFSET
        limit = (stop_price * (1 + offset)).quantize(Decimal("0.01"))
        order |= {"type": "stop_limit", "stop_price": str(stop_price),
                  "limit_price": str(limit)}
    else:
        order |= {"type": "stop", "stop_price": str(stop_price)}
    return order


def open_lot(lot: Lot, broker) -> Lot:
    """Arm a lot: refuse if the symbol is busy, then rest the disaster stop."""
    if assets.is_crypto(lot.symbol):
        resting = [o for o in broker.open_orders(lot.symbol)
                   if o.get("side") == lot.exit_side]
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
