"""Named exit plans. The alert says which; the numbers live here.

Wei's ask: "alert format would be something like EXIT_PLAN=DYNAMIC_TRAIL —
those percentage and target should be stored in a config." That split matters
beyond tidiness. The strategy in TradingView and the ladder in the gateway are
edited by different hands at different times, and putting the numbers in the
alert means every change to a target is a change to the strategy, deployed by
re-saving an alert.

Plans are returned as fresh objects, never a shared instance. A lot snapshots
its plan at fill time so that editing a plan cannot re-price a position already
in the market — handing out one shared object would defeat that from the other
direction, with two lots holding the same one.
"""

from __future__ import annotations

from decimal import Decimal

from .exit_manager import ExitPlan

_PLANS: dict[str, dict] = {
    # 20% at +1.2R, 30% at +2.5R, 50% runner trailing the previous completed
    # bar's low, stop to breakeven once the first rung is done.
    "DYNAMIC_TRAIL": dict(
        tranches=((Decimal("0.20"), Decimal("1.2")),
                  (Decimal("0.30"), Decimal("2.5"))),
        runner_fraction=Decimal("0.50"),
        trail_source="previous_completed_bar_low",
        breakeven_after=1,
        # Wei: "only for the part of dynamic_trail exit plan." Scoped here
        # rather than made global, so the simple protective-stop path and any
        # future plan are unaffected.
        rungs_on_bar_high=True,
        # Re-place the resting broker stop once the software trail has pulled
        # 0.5R ahead of it. Wei's original call was that the disaster stop stays
        # put and the gateway moves its own — correct against per-bar churn, but
        # it leaves the whole trailed gain resting on this process staying
        # alive, and that exposure grows with the size of the win. 0.5R is a
        # handful of orders over a runner's life for a bounded gap.
        stop_ratchet_r=Decimal("0.5"),
    ),
    # A take-profit and a stop, whichever comes first — the shape people mean
    # by "OCO".
    #
    # It is managed HERE, not by Alpaca's order_class="oco". Alpaca's native
    # OCO is equities-only; crypto takes simple orders only, and BTC/USD is
    # what this is being tested on. Managing it ourselves also means one
    # mechanism for both asset classes instead of two that drift.
    #
    # The trade-off against a broker-side OCO is real and worth stating: a
    # native pair survives our process dying, and this does not. The disaster
    # stop still rests at the broker, so the FLOOR survives regardless — what
    # is lost while we are down is the take-profit, which is the side that
    # costs upside rather than capital.
    #
    # The target comes from the alert, not from here. Wei: "I will provide
    # explicit stop and take profit prices on the OCO plan." The R-multiple
    # below is never consulted when TAKE_PROFIT is supplied — and TAKE_PROFIT
    # is required for this plan — so it exists only to keep the tranche shape
    # uniform with every other plan. One tranche, whole position, no runner.
    # The same shape as DYNAMIC_TRAIL with targets you can reach in minutes.
    #
    # Wei had been editing DYNAMIC_TRAIL's multiples in place to test — 1.2R on
    # a 26-cent R needs TSLA to move 65 cents, which can take an afternoon. It
    # worked, and it left the repository saying 1.2R/2.5R while the running
    # gateway used 0.2R/0.4R: the fourth time this week the deployed code
    # differed from master, and the only one /healthz could not have caught,
    # because an uncommitted edit has the same commit hash.
    #
    # A named plan makes testing an alert field instead of a file edit. The
    # real strategy cannot be overwritten by a test value, and test multiples
    # cannot be left running by forgetting to revert them.
    "DYNAMIC_TRAIL_FAST": dict(
        tranches=((Decimal("0.20"), Decimal("0.2")),
                  (Decimal("0.30"), Decimal("0.4"))),
        runner_fraction=Decimal("0.50"),
        trail_source="previous_completed_bar_low",
        breakeven_after=1,
        rungs_on_bar_high=True,
        # Scaled with the multiples: this plan exists so a rung can be reached
        # in minutes, and a 0.5R ratchet on a 0.2R/0.4R ladder would never fire
        # inside a test.
        stop_ratchet_r=Decimal("0.1"),
    ),
    "OCO_AFTER_FILL": dict(
        tranches=((Decimal("1.00"), Decimal("2.0")),),
        runner_fraction=Decimal("0"),
        trail_source="none",
        breakeven_after=0,          # nothing survives the fill to move a stop for
        rungs_on_bar_high=True,     # same thin-feed reasoning as DYNAMIC_TRAIL
    ),
}


def names() -> list[str]:
    return sorted(_PLANS)


def resolve(name: str) -> ExitPlan:
    """The plan by name, as a new object each time.

    Raises rather than falling back to a default: an alert naming a plan that
    does not exist must not quietly become an unmanaged position, and must
    certainly not inherit whichever ladder happens to be first in the table.
    """
    key = (name or "").strip().upper()
    if key not in _PLANS:
        raise KeyError(
            f"unknown EXIT_PLAN {name!r}; known plans are {', '.join(names())}")
    return ExitPlan(name=key, **_PLANS[key])
