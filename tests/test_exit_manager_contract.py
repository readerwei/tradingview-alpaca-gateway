"""Contract for gateway-side exit management: scale out, then trail.

WHAT WEI ASKED FOR
------------------
    TP1     sell 20% at +1.2R
    TP2     sell 30% at +2.5R
    runner       50%          trail the previous completed bar's low
    after TP1    stop -> breakeven
    lots         independent — one alert's exits never touch another's
    alert        EXIT_PLAN=DYNAMIC_TRAIL; the numbers live in config

and, when asked whether the decision should live in Pine or in the gateway:
"I need it to exist in the gateway." That choice is what makes this file
necessary. A Pine-side ladder is just more alerts, and the existing execution
contract already covers those. A gateway-side ladder is a stateful live system
that holds an open position across restarts, so its rules have to be written
down somewhere they can fail.

WHY NOT EXTEND order_manager.py
-------------------------------
There is already an ``ExitManager`` on master. Its shape is right — a
deterministic core with an injected broker and no credentials of its own — and
that shape is kept here. Its decisions are not:

* it submits every take-profit as a **resting limit order** at ``start()``. On
  crypto a resting sell blocks a new buy (Alpaca refuses at submission and
  leaves no order record) and reserves quantity, so a two-rung ladder locks the
  symbol out of every subsequent entry;
* the runner uses Alpaca's ``trailing_stop`` **order type**, which crypto does
  not support at all, and which is a fixed distance — the opposite of the
  bar-low trail asked for;
* take-profits are absolute prices. R is not knowable until the entry fills, so
  something has to compute the ladder at fill time from the fill price and the
  initial stop;
* ``remaining_qty``/``filled_qty`` are typed ``int``. The annotations do not
  bind at runtime, so this is not what would break first — but a manager for an
  asset whose tranches are 0.00029925 should not be written as though shares
  were the unit;
* there is no breakeven move, no persistence (``processed_fills`` is a set in
  RAM), and no lot identity, so nothing survives a restart and independent lots
  cannot be expressed.

``tests/test_order_manager.py`` passes today and would fail on the first real
crypto order, because ``FakeOrderBroker.submit_trailing_stop`` accepts a call
that Alpaca rejects. That is the same defect that shipped an ``AlpacaPaperClient``
with no ``submit_order``: the fake is normative and nothing checks the real path.
Hence ``test_the_adapter_refuses_what_alpaca_refuses`` below, which asserts
against the asset rules rather than against a fake's good manners.

THE DESIGN THIS PINS
--------------------
Two tiers, because software-managed exits die when the process does:

    broker  ONE resting stop per lot, at the original stop. It is a disaster
            floor, not the working stop. It moves only to be RESIZED smaller
            after a partial fill, and is never widened.
    gateway breakeven and the bar-low trail, held as numbers in the DB and
            fired as market sells when breached.

So the working stop costs no broker round-trip to move, and the only cancel/
replace window is the resize after a fill — where the existing retry-then-
flatten path already applies. If the gateway is down during a move, the outcome
is a bad exit rather than an unbounded one.

ONE OPEN QUESTION IS ASSUMED, NOT ANSWERED
------------------------------------------
Independent lots + a resting broker stop + crypto's wash-trade rule cannot all
hold: while lot A's stop rests, entry B is refused. This file assumes
**one lot at a time per crypto symbol**, and confines that assumption to
``test_a_second_crypto_entry_is_refused_while_a_lot_is_open``. If Wei picks
multi-lot instead, that test is the edit.

Every refusal below is matched on its reason. A bare ``raises(Exception)``
passes against a module that does not exist yet for reasons that have nothing
to do with the rule under test — which has now happened four times in this
repo, each time in a test written specifically to catch it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

manager = pytest.importorskip(
    "tv_alpaca_gateway.exit_manager",
    reason="gateway-side exit management not built yet")

# The live lot at the time of writing, so the numbers below are real rather
# than invented:  entry 64960.58, stop 64100  ->  R = 860.58
ENTRY = Decimal("64960.58")
STOP = Decimal("64100")
HELD = Decimal("0.00149625")          # 0.0015 less Alpaca's in-kind entry fee
R = ENTRY - STOP                      # 860.58
TP1_PRICE = ENTRY + Decimal("1.2") * R    # 65993.276
TP2_PRICE = ENTRY + Decimal("2.5") * R    # 67112.03
MIN_ORDER = Decimal("0.000015417")    # BTC/USD min_order_size, from /v2/assets

PLAN = dict(
    name="DYNAMIC_TRAIL",
    tranches=((Decimal("0.20"), Decimal("1.2")), (Decimal("0.30"), Decimal("2.5"))),
    runner_fraction=Decimal("0.50"),
    trail_source="previous_completed_bar_low",
    breakeven_after=1,
)


def _lot(**over):
    fields = dict(event_id="evt-1", symbol="BTC/USD", entry_price=ENTRY,
                  initial_stop=STOP, held_qty=HELD, timeframe="5m",
                  plan=manager.ExitPlan(**PLAN), min_order_size=MIN_ORDER)
    fields.update(over)
    return manager.Lot.opened(**fields)


# ═══════════════════════════════════════════════════ sizing the ladder at fill

def test_the_ladder_is_computed_from_the_fill_price_not_the_signal_price():
    """R is entry minus stop, and entry is not known until the fill.

    A market order into a fast tape can fill well away from the price the bar
    closed at. Sizing the ladder off the signal price would put the targets at
    the wrong distance from the risk actually taken, which is the one thing an
    R-multiple ladder exists to get right.
    """
    lot = _lot()

    assert lot.risk_per_unit == R
    assert lot.target_price(1) == pytest.approx(TP1_PRICE)
    assert lot.target_price(2) == pytest.approx(TP2_PRICE)


def test_the_tranches_sum_to_exactly_the_held_quantity():
    """20/30/50 of an odd quantity does not divide cleanly.

    Rounding each leg independently either strands dust that can never be sold
    or oversells into a quantity the account does not hold. The runner has to
    absorb the remainder.
    """
    lot = _lot()
    tranches = [lot.tranche_qty(1), lot.tranche_qty(2), lot.runner_qty()]

    assert sum(tranches) == HELD, (
        f"tranches sum to {sum(tranches)} against a held quantity of {HELD}")


def test_a_plan_whose_smallest_tranche_is_below_the_minimum_is_refused_up_front():
    """Checked when the plan is made, not when the rung fires.

    A ladder that passes TP1 and then cannot place TP2 leaves a position half
    managed, with a remainder the manager will keep trying and failing to sell.
    The moment to find that out is before the first order, not in the middle.
    """
    with pytest.raises(manager.ExitPlanError, match=r"(?i)min_order_size|minimum|too small"):
        _lot(held_qty=Decimal("0.00005"))     # 20% of this is under the floor


def test_no_rung_may_leave_an_unsellable_remainder():
    """The mirror of the rule above.

    Every tranche can be big enough while still leaving a runner below the
    floor — dust that pins the lot open forever because the final exit can
    never be submitted.
    """
    lot = _lot()
    for remaining in (lot.held_qty - lot.tranche_qty(1),
                      lot.held_qty - lot.tranche_qty(1) - lot.tranche_qty(2)):
        assert remaining == 0 or remaining >= MIN_ORDER, (
            f"a rung leaves {remaining}, below the {MIN_ORDER} floor")


def test_the_plan_is_snapshotted_onto_the_lot():
    """Editing the config must not re-price a position that is already open.

    Otherwise a change made while a trade is running silently moves targets the
    trade was entered under, and the record of why it exited stops matching the
    rules it exited by.
    """
    config = dict(PLAN)
    lot = _lot(plan=manager.ExitPlan(**config))
    before = lot.target_price(1)

    config["tranches"] = ((Decimal("0.20"), Decimal("9.9")),)   # someone edits the config

    assert lot.target_price(1) == before, "an open lot re-priced itself from config"


# ══════════════════════════════════════════════════════════ the two-tier stop

def test_the_broker_holds_one_resting_stop_and_never_a_resting_take_profit():
    """The design decision this file exists to protect.

    Every resting sell on a crypto symbol blocks the next buy and reserves
    quantity. A ladder parked at the broker as limit orders locks the symbol.
    So: exactly one broker order per lot — the disaster stop.
    """
    broker = _RecordingBroker()
    manager.open_lot(_lot(), broker)

    resting = [o for o in broker.submitted if o["side"] == "sell"]
    assert len(resting) == 1, f"expected one resting sell, got {len(resting)}: {resting}"
    assert resting[0]["type"] in ("stop", "stop_limit")
    assert Decimal(str(resting[0]["qty"])) == HELD


def test_moving_the_working_stop_touches_no_broker_order():
    """Breakeven and the trail live in the DB.

    If moving the working stop meant a cancel-and-replace, every bar close on
    a trailing runner would open a window with no protection at the broker, on
    a schedule. Keeping the disaster stop still and the working stop in
    software means the only cancel/replace is the resize after a fill.
    """
    broker = _RecordingBroker()
    lot = manager.open_lot(_lot(), broker)
    before = len(broker.submitted) + len(broker.cancelled)

    lot.on_bar(high=ENTRY + R, low=ENTRY + Decimal("100"), close=ENTRY + R)

    assert len(broker.submitted) + len(broker.cancelled) == before, (
        "moving the working stop went to the broker")


def test_the_disaster_stop_is_resized_down_after_a_partial_fill_and_never_widened():
    """An oversized sell stop is rejected on trigger — the failure surfaces at
    exactly the moment protection is needed. And a stop that grows back would
    try to sell coins another lot owns."""
    broker = _RecordingBroker()
    lot = manager.open_lot(_lot(), broker)

    lot.on_price(TP1_PRICE)                       # TP1 fires, 20% gone
    lot.on_fill(rung=1, filled_qty=lot.tranche_qty(1))

    stops = [o for o in broker.submitted if o["type"] in ("stop", "stop_limit")]
    assert Decimal(str(stops[-1]["qty"])) == HELD - lot.tranche_qty(1)
    assert all(Decimal(str(a["qty"])) <= Decimal(str(b["qty"]))
               for a, b in zip(stops[1:], stops)), "the disaster stop was widened"


def test_the_stop_moves_to_breakeven_after_the_first_target():
    lot = manager.open_lot(_lot(), _RecordingBroker())
    assert lot.working_stop == STOP

    lot.on_price(TP1_PRICE)
    lot.on_fill(rung=1, filled_qty=lot.tranche_qty(1))

    assert lot.working_stop == ENTRY, "the stop did not move to breakeven after TP1"


# ═════════════════════════════════════════════════════════════════ the trail

def test_the_trail_uses_the_previous_completed_bar_not_the_forming_one():
    """A stop that follows the live candle ratchets up on every tick inside the
    bar and exits on noise that the bar itself would have closed above."""
    lot = manager.open_lot(_lot(), _RecordingBroker())
    lot.advance_to_runner()

    lot.on_bar(high=ENTRY + 3 * R, low=ENTRY + 2 * R, close=ENTRY + 3 * R)
    settled = lot.working_stop
    lot.on_price(ENTRY + Decimal("4000"))          # mid-bar spike, bar not closed

    assert lot.working_stop == settled, "the trail moved on an unfinished bar"


def test_the_trail_only_ever_moves_up():
    """A pullback lowering the stop would give back locked-in profit and, at
    the limit, walk the stop back below the entry it was protecting."""
    lot = manager.open_lot(_lot(), _RecordingBroker())
    lot.advance_to_runner()

    lot.on_bar(high=ENTRY + 3 * R, low=ENTRY + 2 * R, close=ENTRY + 3 * R)
    high_water = lot.working_stop
    lot.on_bar(high=ENTRY + 2 * R, low=ENTRY, close=ENTRY + R)

    assert lot.working_stop == high_water, "the trail loosened on a pullback"


def test_a_bar_with_no_trades_does_not_move_the_trail():
    """Measured, not assumed. Twelve hours of Alpaca's BTC/USD 1m bars:

        1Min    479/719 minutes have a bar,  167 of those have any trades
        5Min    142/144                       92
        15Min    48/48                        46

    Two thirds of the 1m bars are built from quotes, many with low == high.
    Trailing off them ratchets the stop to a price nothing traded at, and the
    next spread wobble takes the position out.
    """
    lot = manager.open_lot(_lot(), _RecordingBroker())
    lot.advance_to_runner()
    lot.on_bar(high=ENTRY + 3 * R, low=ENTRY + 2 * R, close=ENTRY + 3 * R, trade_count=41)
    settled = lot.working_stop

    lot.on_bar(high=ENTRY + 4 * R, low=ENTRY + 3 * R, close=ENTRY + 4 * R, trade_count=0)

    assert lot.working_stop == settled, "the trail followed a bar with no trades"


def test_the_trail_timeframe_comes_from_the_alert():
    """"Last bar's low" is meaningless without a bar size, and a configured
    default would silently trail a 1h signal on 5m bars — a stop four times
    tighter than the strategy was tested with."""
    assert _lot(timeframe="1h").timeframe == "1h"
    with pytest.raises(manager.ExitPlanError, match=r"(?i)timeframe|interval"):
        _lot(timeframe=None)


# ═════════════════════════════════════════ firing a rung: once, and no more

def test_a_rung_carries_a_deterministic_client_order_id():
    """This is what makes a double-fire impossible rather than unlikely.

    A crash between deciding to sell and hearing back from Alpaca leaves no
    local record, so on restart the manager sees the target still breached.
    Alpaca rejects a duplicate client_order_id, so the retry is refused by the
    broker instead of doubling the exit.
    """
    lot = _lot(event_id="evt-7")
    assert lot.rung_client_order_id(1) == "pine-exec-evt-7-tp1"
    assert lot.rung_client_order_id(1) == lot.rung_client_order_id(1)
    assert lot.rung_client_order_id(2) != lot.rung_client_order_id(1)


def test_a_rung_already_filled_does_not_fire_again_when_price_revisits_it():
    """Price crossing 1.2R, falling back, and crossing again is the normal case,
    not an edge case."""
    broker = _RecordingBroker()
    lot = manager.open_lot(_lot(), broker)

    lot.on_price(TP1_PRICE)
    lot.on_fill(rung=1, filled_qty=lot.tranche_qty(1))
    lot.on_price(ENTRY + Decimal("10"))
    lot.on_price(TP1_PRICE)

    sells = [o for o in broker.submitted if o.get("client_order_id", "").endswith("-tp1")]
    assert len(sells) == 1, f"TP1 fired {len(sells)} times"


def test_a_partially_filled_rung_is_topped_up_not_written_off():
    """Wei: "I want the whole tranche managed."

    A market order for 3 QQQ can come back as 1 and then 2. Treating the first
    fill as completion strands the rest outside the ladder — and worse, resizes
    the stop as though the whole tranche had sold, leaving the position
    under-protected by the part that never went.
    """
    broker = _RecordingBroker()
    lot = manager.open_lot(_lot(), broker)
    tranche = lot.tranche_qty(1)
    broker.next_fill_ratio = Decimal("1") / 3            # only a third trades

    lot.on_price(TP1_PRICE)
    part = Decimal(broker.filled[-1]["filled_qty"])
    lot.on_fill(rung=1, filled_qty=part)

    assert not lot.rung_filled(1), "a partial fill closed the rung"
    assert lot.working_stop == STOP, "breakeven applied before the tranche was done"

    lot.on_price(TP1_PRICE)                              # still breached: top up
    sold = sum(Decimal(o["filled_qty"]) for o in broker.filled
               if "-tp1" in o["client_order_id"])
    assert sold == tranche, (
        f"the rung sold {sold} of a {tranche} tranche and stopped")


def test_a_topped_up_rung_does_not_reuse_the_first_order_id():
    """The remainder is a genuinely new order. Reusing the id would have Alpaca
    reject it as a duplicate — the same mechanism that protects against a
    double-fire would here prevent the tranche from ever completing."""
    lot = _lot(event_id="evt-7")
    assert lot.rung_client_order_id(1) == "pine-exec-evt-7-tp1"
    assert lot.rung_client_order_id(1, attempt=1) != lot.rung_client_order_id(1)


def test_an_exit_is_never_larger_than_what_the_account_actually_holds():
    """Independent lots are our fiction; the broker has one position per symbol.

    If lot accounting drifts, an exit sized from the DB sells coins belonging
    to another lot — or fails outright. Clamping to the real position turns a
    silent accounting bug into a small, visible one.
    """
    broker = _RecordingBroker(position=Decimal("0.0005"))    # less than the lot believes
    lot = manager.open_lot(_lot(), broker)

    lot.on_price(TP2_PRICE)

    for order in broker.submitted:
        if order["side"] == "sell":
            assert Decimal(str(order["qty"])) <= Decimal("0.0005"), (
                "an exit was sized past the real position")


# ══════════════════════════ reservation: what the first fake was too kind to see

def test_the_stop_is_resized_before_the_tranche_sells_not_after():
    """Found by teaching the fake about reserved quantity, not by reasoning.

    A resting stop holds its whole quantity, so with the stop covering the
    position there is nothing available for a take-profit to sell. Something
    has to give the quantity back first. The order chosen here — cancel,
    re-place at the size the lot will have once the rung fills, then sell —
    leaves one short gap instead of two, and leaves the stop already correct
    if the sell never lands.

    Cancel -> sell -> re-place would strand the entire position naked across
    two round-trips at exactly the moment price is moving.
    """
    broker = _RecordingBroker()
    lot = manager.open_lot(_lot(), broker)

    lot.on_price(TP1_PRICE)

    kinds = [(o["type"], Decimal(str(o["qty"]))) for o in broker.submitted]
    assert kinds[0][0] in ("stop", "stop_limit") and kinds[0][1] == HELD
    assert kinds[1][0] in ("stop", "stop_limit"), (
        f"the tranche sold before the stop was resized: {kinds}")
    assert kinds[1][1] == HELD - lot.tranche_qty(1), (
        "the replacement stop was not sized to the post-fill position")
    assert kinds[2][0] == "market" and kinds[2][1] == lot.tranche_qty(1)


def test_the_final_exit_cancels_the_stop_that_is_holding_the_coins():
    """The stop reserves exactly the quantity the exit needs to sell.

    Without cancelling it first the flatten is rejected for insufficient
    quantity — a stop breach that fails to exit, which is the worst possible
    moment to discover a reservation rule.
    """
    broker = _RecordingBroker()
    lot = manager.open_lot(_lot(), broker)
    lot.advance_to_runner()                      # working stop is now breakeven
    lot.on_bar(high=ENTRY + 3 * R, low=ENTRY + R, close=ENTRY + 3 * R)

    lot.on_price(ENTRY + R - Decimal("1"))       # trail breached

    assert broker.cancelled, "the resting stop was never cancelled"
    exits = [o for o in broker.submitted if o["type"] == "market"]
    assert exits and Decimal(str(exits[-1]["qty"])) == HELD


def test_the_software_stop_stays_out_of_the_way_of_the_resting_one():
    """Before breakeven the two stops sit at the same price.

    Selling here as well would race our own resting order for the same coins:
    the broker stop triggers, the software fires a market sell, and one of them
    is rejected — or on an asset that can go short, is not. The resting stop is
    already at that level and does not need us alive, so software does nothing
    until its stop is strictly tighter.
    """
    broker = _RecordingBroker()
    lot = manager.open_lot(_lot(), broker)

    lot.on_price(STOP - Decimal("1"))

    assert not broker.cancelled, "software raced the resting stop at its own price"
    assert [o["type"] for o in broker.submitted] == ["stop_limit"]


# ═══════════════════════════════════════════════════════ surviving a restart

def test_state_is_rebuilt_from_the_broker_not_trusted_from_the_database():
    """Runtime state has diverged from what we believed was running four times
    this week. The account is the fact; the DB is a cache of it.

    A lot recorded as 80% open against a flat account must come back closed,
    not re-arm a ladder against coins that are gone.
    """
    broker = _RecordingBroker(position=Decimal("0"))
    stored = _lot()

    rebuilt = manager.reconcile_lot(stored, broker)

    assert rebuilt.is_closed, "a lot survived reconciliation against a flat account"


def test_reconciliation_recovers_a_rung_that_filled_while_we_were_down():
    """The gap this closes: the manager fires TP1, the process dies before the
    fill callback, and on restart the DB still shows the rung pending. Without
    reading the rung's order back, the ladder re-fires it."""
    broker = _RecordingBroker(position=HELD - Decimal("0.00029925"))
    broker.orders["pine-exec-evt-1-tp1"] = {"status": "filled",
                                            "filled_qty": "0.00029925"}

    rebuilt = manager.reconcile_lot(_lot(), broker)

    assert rebuilt.rung_filled(1)
    assert rebuilt.working_stop == ENTRY, "breakeven was not applied on recovery"


def test_reconciliation_does_not_write_back_into_the_lot_it_read():
    """`dataclasses.replace` passes the stored lot's own sets straight through.

    Sharing them means the rebuilt lot edits the record it was rebuilt from, so
    a reconciliation that turns out to be wrong has already overwritten the
    evidence of what was stored.
    """
    stored = _lot()
    rebuilt = manager.reconcile_lot(stored, _RecordingBroker())

    rebuilt.filled_rungs.add(2)

    assert 2 not in stored.filled_rungs, "reconciliation mutated the stored lot"


# ══════════════════════════════════════════════ crypto is not equities at night

def test_the_adapter_refuses_what_alpaca_refuses():
    """Asserted against the asset rules, not against a fake's good manners.

    ``FakeOrderBroker.submit_trailing_stop`` in test_order_manager.py accepts a
    ``trailing_stop`` on BTC/USD and returns an id. Alpaca does not support the
    type on crypto at all, so that test is green about an order that cannot
    exist. The runner here trails in software precisely so this order type is
    never needed.
    """
    from tv_alpaca_gateway import assets

    assert assets.is_crypto("BTC/USD")
    assert assets.time_in_force("BTC/USD") == "gtc"
    with pytest.raises(Exception, match=r"(?i)trailing|not supported|crypto"):
        manager.build_stop_order("BTC/USD", Decimal("0.001"), STOP, trail_percent=Decimal("2"))


def test_a_second_crypto_entry_is_refused_while_a_lot_is_open():
    """ASSUMPTION, pending Wei's call — see the module docstring.

    Independent lots, a resting broker stop, and crypto's wash-trade rule
    cannot all hold at once. Refusing the second entry is the option that keeps
    a real stop at the broker; it is also the one that fails loudly rather than
    letting Alpaca reject the buy with no order record at all.
    """
    broker = _RecordingBroker()
    manager.open_lot(_lot(event_id="evt-1"), broker)

    with pytest.raises(manager.LotConflict, match=r"(?i)open lot|already|resting"):
        manager.open_lot(_lot(event_id="evt-2"), broker)


# ═════════════════════════════════════════════════════════════════════ fake

class InsufficientQty(RuntimeError):
    """What Alpaca returns when an order asks for quantity that is spoken for."""


class _RecordingBroker:
    """Dumber than the real adapter in every way except one.

    It models **quantity reservation**, because that is the rule the manager
    has to be right about and the one a permissive fake hides. A resting sell
    holds its quantity until it is cancelled or filled; `qty_available` falls
    to zero under a full-size stop, which is exactly what the live account
    shows:

        qty            0.00149625
        qty_available  0

    The first version of this fake accepted any sell, and every test below was
    green against a manager that would have been rejected by Alpaca on its
    first take-profit. That is the fifth time in this repo a double has been
    more agreeable than the broker; it is worth the twenty lines.
    """

    def __init__(self, position: Decimal = HELD):
        self.submitted: list[dict] = []
        self.filled: list[dict] = []
        self.cancelled: list[str] = []
        self.orders: dict[str, dict] = {}
        self.next_fill_ratio = Decimal("1")
        self._position = position
        self._resting: dict[str, Decimal] = {}

    def available(self) -> Decimal:
        return self._position - sum(self._resting.values())

    def submit_order(self, **kwargs):
        qty = Decimal(str(kwargs["qty"]))
        if kwargs["side"] == "sell" and qty > self.available():
            raise InsufficientQty(
                f"insufficient qty available for order (requested: {qty}, "
                f"available: {self.available()})")

        self.submitted.append(dict(kwargs))
        order_id = f"ord-{len(self.submitted)}"
        if kwargs.get("type") == "market":
            # A market order fills and is done; any unfilled remainder is
            # cancelled rather than left resting. `next_fill_ratio` models the
            # partial case, where the position falls by what actually traded
            # and not by what was asked for.
            filled = (qty * self.next_fill_ratio).quantize(Decimal("1E-9"))
            self.next_fill_ratio = Decimal("1")
            self._position -= filled
            self.filled.append({**kwargs, "filled_qty": str(filled)})
            return {"id": order_id,
                    "status": "filled" if filled == qty else "partially_filled",
                    "filled_qty": str(filled)}
        self._resting[order_id] = qty      # holds its quantity until cancelled
        return {"id": order_id, "status": "new", "filled_qty": "0"}

    def cancel_order(self, order_id: str) -> None:
        self.cancelled.append(order_id)
        self._resting.pop(order_id, None)

    def position_qty(self, symbol: str) -> Decimal:
        return self._position

    def get_order_by_client_id(self, client_order_id: str) -> dict | None:
        return self.orders.get(client_order_id)

    def open_orders(self, symbol: str) -> list[dict]:
        resting_ids = {f"ord-{i + 1}" for i in range(len(self.submitted))
                       if f"ord-{i + 1}" in self._resting}
        return [o for i, o in enumerate(self.submitted)
                if f"ord-{i + 1}" in resting_ids]
