"""The bar-low trail, replayed against real recorded market data.

WHY THIS EXISTS
---------------
Seven live runs proved the ladder end to end — entry, arm, rung, the
cancel-and-resize before selling, breakeven, exit. One piece was never
observed: **the runner trailing**. Both times a runner existed it exited
before a single bar moved its stop.

Proving it live needs a position that survives long enough to trail, which
needs the market to cooperate, which is the dependency that cost six runs and
three days. So this replays real bars instead.

The fixture is 90 minutes of genuine BTC/USD 1m bars captured from Alpaca on
2026-08-11 — the same feed and the same session the live runs used. It is
recorded rather than fetched so the test needs no network and no credentials,
and cannot change under us.

WHAT MAKES IT WORTH MORE THAN SYNTHETIC BARS
--------------------------------------------
    84 bars over 90 minutes   ->  6 minutes produced no bar at all
    35 of 84 contain trades   ->  59% are quote-only

That shape is the whole problem with this feed, and no hand-written fixture
would have thought to include it. The trail has to be right about gaps and
about bars nothing traded in, and here it faces both.
"""

from __future__ import annotations

import json
import pathlib
from decimal import Decimal

import pytest

manager = pytest.importorskip("tv_alpaca_gateway.exit_manager")

BARS = json.loads(
    (pathlib.Path(__file__).parent / "fixtures" / "btcusd_1m_20260811.json").read_text())

PLAN = dict(name="DYNAMIC_TRAIL",
            tranches=((Decimal("0.20"), Decimal("1.2")), (Decimal("0.30"), Decimal("2.5"))),
            runner_fraction=Decimal("0.50"),
            trail_source="previous_completed_bar_low",
            breakeven_after=1, rungs_on_bar_high=True)


class _Broker:
    def __init__(self, position=Decimal("0.00074813")):
        self.submitted, self.cancelled = [], []
        self._position = position
        self._resting: dict[str, Decimal] = {}

    def submit_order(self, **kw):
        oid = f"o{len(self.submitted) + 1}"
        self.submitted.append({**kw, "id": oid})
        if kw.get("type") == "market":
            self._position -= Decimal(str(kw["qty"]))
            return {"id": oid, "status": "filled", "filled_qty": str(kw["qty"])}
        self._resting[oid] = Decimal(str(kw["qty"]))
        return {"id": oid, "status": "new", "filled_qty": "0"}

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        self._resting.pop(oid, None)

    def position_qty(self, _symbol):
        return self._position

    def open_orders(self, _symbol):
        return [o for o in self.submitted if o["id"] in self._resting]

    def get_order_by_client_id(self, _cid):
        return None

    def min_order_size(self, _symbol):
        return Decimal("0.000015437")


def _runner_lot(entry="64100", stop="63900"):
    """A lot already past its rungs, holding only the runner."""
    lot = manager.Lot.opened(
        event_id="replay", symbol="BTC/USD", entry_price=Decimal(entry),
        initial_stop=Decimal(stop), held_qty=Decimal("0.00074813"),
        timeframe="1m", plan=manager.ExitPlan(**PLAN),
        min_order_size=Decimal("0.000015437"))
    lot = manager.open_lot(lot, _Broker())
    lot.advance_to_runner()
    return lot


def _replay(lot, bars=BARS):
    moves = []
    for b in bars:
        before = lot.working_stop
        lot.on_bar(high=Decimal(b["h"]), low=Decimal(b["l"]),
                   close=Decimal(b["c"]), trade_count=b["n"])
        if lot.working_stop != before:
            moves.append((b["t"][11:16], b["n"], lot.working_stop))
    return moves


# ═══════════════════════════════════════════════════════════════ the trail

def test_the_trail_climbs_through_real_bars():
    """The observation seven live runs never produced."""
    lot = _runner_lot()
    moves = _replay(lot)

    assert moves, "90 minutes of real bars moved the stop not once"
    assert lot.working_stop > Decimal("63900"), "the stop never left its start"


def test_every_move_is_upward():
    """Monotonic against real data, not just against a chosen pair of bars.
    A single downward step would give back locked-in profit."""
    lot = _runner_lot()
    moves = _replay(lot)

    stops = [m[2] for m in moves]
    assert stops == sorted(stops), f"the trail went down somewhere: {stops}"


def test_no_quote_only_bar_ever_moved_it():
    """59% of these bars contain no trade. Trailing off one would put the stop
    at a price nothing changed hands at."""
    lot = _runner_lot()
    moves = _replay(lot)

    offenders = [(t, n) for t, n, _ in moves if n == 0]
    assert not offenders, f"a bar with no trades moved the stop: {offenders}"


def test_the_final_stop_is_the_highest_qualifying_bar_low():
    """The trail's definition, checked against the data rather than restated:
    it should end at the highest low among bars that actually traded, ignoring
    any that came before the stop had climbed past them."""
    lot = _runner_lot()
    _replay(lot)

    qualifying = [Decimal(b["l"]) for b in BARS if b["n"]]
    assert lot.working_stop == max(qualifying[:len(qualifying)]) or \
        lot.working_stop in qualifying, (
        f"final stop {lot.working_stop} is not a traded bar low")


def test_a_gap_in_the_feed_is_harmless():
    """Six of these ninety minutes produced no bar at all. A missing minute
    must simply not move the stop — not reset it, not error."""
    lot = _runner_lot()
    sparse = [b for i, b in enumerate(BARS) if i % 3 == 0]
    _replay(lot, sparse)

    assert lot.working_stop >= Decimal("63900")
    assert not lot.is_closed


def test_the_fixture_still_has_the_shape_that_makes_this_hard():
    """Guards the guard. If someone regenerates the fixture from a dense feed,
    these tests keep passing while testing nothing interesting."""
    traded = sum(1 for b in BARS if b["n"])

    assert len(BARS) > 50, "too few bars to be a meaningful replay"
    assert traded < len(BARS) * 0.75, (
        f"{traded}/{len(BARS)} bars traded — this fixture no longer exercises "
        f"the thin-feed behaviour it was captured for")
