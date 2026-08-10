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
