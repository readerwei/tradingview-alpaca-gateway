"""Contract for the seam between the streams and the ladder.

`exit_manager` decides what a lot does; the supervisor decides which lot is
asked. That sounds like plumbing, and it is where the expensive mistakes live:
a fill routed to the wrong rung is, from inside the lot, indistinguishable from
a fill that really happened. The lot will resize its own stop on the strength
of it.

The bar/price feed was the last unwired piece, and two constraints shape it:

* **one market-data connection per feed, for the whole account.** A probe
  tonight came back `connection limit exceeded` on IEX while something else
  held the slot, so a second socket just for bars is not an option — bars are
  added to the existing subscription instead.
* **`trade_updates` are cumulative, and Alpaca does not replay what was missed
  while the socket was down.** So a fill message carries the running total for
  the order, not the increment, and the timer exists because a stream that
  dropped and came back leaves state that looks fine and is not.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

supervisor = pytest.importorskip("tv_alpaca_gateway.lot_supervisor")

from tv_alpaca_gateway import exit_manager as manager   # noqa: E402
from tv_alpaca_gateway.store import EventStore          # noqa: E402

ENTRY = Decimal("64960.58")
STOP = Decimal("64100")
HELD = Decimal("0.00149625")
R = ENTRY - STOP
TP1 = ENTRY + Decimal("1.2") * R
MIN_ORDER = Decimal("0.000015417")

PLAN = dict(name="DYNAMIC_TRAIL",
            tranches=((Decimal("0.20"), Decimal("1.2")), (Decimal("0.30"), Decimal("2.5"))),
            runner_fraction=Decimal("0.50"),
            trail_source="previous_completed_bar_low", breakeven_after=1)


def _lot(**over):
    fields = dict(event_id="evt-1", symbol="BTC/USD", entry_price=ENTRY,
                  initial_stop=STOP, held_qty=HELD, timeframe="1m",
                  plan=manager.ExitPlan(**PLAN), min_order_size=MIN_ORDER)
    fields.update(over)
    return manager.Lot.opened(**fields)


class _Broker:
    def __init__(self, position=HELD):
        self.submitted, self.cancelled, self.orders = [], [], {}
        self._position = position
        self._resting: dict[str, Decimal] = {}

    def submit_order(self, **kw):
        order_id = f"ord-{len(self.submitted) + 1}"
        self.submitted.append({**kw, "id": order_id})
        if kw.get("type") == "market":
            self._position -= Decimal(str(kw["qty"]))
            return {"id": order_id, "status": "filled", "filled_qty": str(kw["qty"])}
        self._resting[order_id] = Decimal(str(kw["qty"]))
        return {"id": order_id, "status": "new", "filled_qty": "0"}

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        self._resting.pop(order_id, None)

    def position_qty(self, symbol):
        return self._position

    def get_order_by_client_id(self, cid):
        return self.orders.get(cid)

    def open_orders(self, symbol):
        return [o for o in self.submitted if o["id"] in self._resting]

    def recent_bars(self, symbol, timeframe, limit=30):
        return list(getattr(self, "bars", []))


class _Update:
    """Shaped like stream.OrderUpdate, only as far as the supervisor reads it."""

    def __init__(self, client_order_id, filled_qty, symbol="BTC/USD",
                 order_id="ord-9", execution_id=None, is_fill=True):
        self.client_order_id = client_order_id
        self.filled_qty = str(filled_qty)
        self.symbol = symbol
        self.order_id = order_id
        self.is_fill = is_fill
        self.raw = {"data": {"execution_id": execution_id} if execution_id else {}}


def _supervisor(tmp_path, broker=None):
    return supervisor.LotSupervisor(EventStore(tmp_path / "s.sqlite3"),
                                    broker or _Broker())


# ═══════════════════════════════════════════════════════════ routing an id

def test_a_rung_is_recognised_by_its_whole_id_not_a_substring():
    """The protective order for an event named `abc-tp1` would otherwise parse
    as rung 1 of event `abc` — and a stop fill would be booked as a take-profit,
    resizing the stop that just triggered."""
    assert supervisor.rung_of("pine-exec-evt-1-tp2") == ("evt-1", 2)
    assert supervisor.rung_of("pine-exec-evt-1-tp2r1") == ("evt-1", 2)
    assert supervisor.rung_of("pine-exec-evt-1-protection-0") is None
    assert supervisor.rung_of("pine-exec-evt-1") is None
    assert supervisor.rung_of("") is None


def test_two_executions_of_one_order_get_different_identities():
    """`trade_updates` carry a cumulative filled quantity, so an order that
    fills in two parts produces two messages with the same order id. Keying the
    dedupe on the order alone would drop every partial after the first."""
    first = supervisor.fill_identity(_Update("pine-exec-evt-1-tp1", "1"))
    second = supervisor.fill_identity(_Update("pine-exec-evt-1-tp1", "3"))
    assert first != second

    with_execution = supervisor.fill_identity(
        _Update("pine-exec-evt-1-tp1", "3", execution_id="exec-77"))
    assert with_execution == "exec-77"


# ═══════════════════════════════════════════════════════ routing an event

def test_a_fill_reaches_the_rung_that_caused_it(tmp_path):
    broker = _Broker()
    sup = _supervisor(tmp_path, broker)
    lot = sup.adopt(manager.open_lot(_lot(), broker))
    lot.on_price(TP1)

    sup.on_order_update(_Update("pine-exec-evt-1-tp1", lot.tranche_qty(1)))

    assert lot.rung_filled(1)
    assert lot.working_stop == ENTRY, "breakeven did not follow the routed fill"


def test_a_cumulative_fill_is_applied_as_an_increment(tmp_path):
    """Alpaca reports the running total for the order, not what just traded.

    Passing that straight through as an increment double-counts every partial:
    a rung that filled 1 then 3 would be recorded as having sold 4.
    """
    broker = _Broker()
    sup = _supervisor(tmp_path, broker)
    lot = sup.adopt(manager.open_lot(_lot(), broker))
    tranche = lot.tranche_qty(1)
    lot.on_price(TP1)

    sup.on_order_update(_Update("pine-exec-evt-1-tp1", tranche / 3, execution_id="e1"))
    sup.on_order_update(_Update("pine-exec-evt-1-tp1", tranche, execution_id="e2"))

    assert lot.rung_filled_qty[1] == tranche, (
        f"cumulative reporting was double-counted: {lot.rung_filled_qty[1]}")


def test_a_non_fill_update_does_not_consume_the_dedupe_identity(tmp_path):
    """An `accepted` update for the same order carries filled_qty 0. Counting it
    as an execution burns the identity the real fill needs, and the fill is then
    silently dropped as a duplicate."""
    broker = _Broker()
    sup = _supervisor(tmp_path, broker)
    lot = sup.adopt(manager.open_lot(_lot(), broker))
    lot.on_price(TP1)

    sup.on_order_update(_Update("pine-exec-evt-1-tp1", "0", is_fill=False))
    sup.on_order_update(_Update("pine-exec-evt-1-tp1", lot.tranche_qty(1)))

    assert lot.rung_filled(1), "the real fill was swallowed"


def test_the_disaster_stop_firing_closes_the_lot_without_waiting_for_the_timer(tmp_path):
    """The one event that ends a lot without the lot deciding anything.

    Left to the reconcile timer, the gateway would keep arming rungs against
    coins that had already been sold, for up to a whole interval.
    """
    broker = _Broker()
    sup = _supervisor(tmp_path, broker)
    lot = sup.adopt(manager.open_lot(_lot(), broker))
    broker._position = Decimal("0")                  # the stop took the position

    sup.on_order_update(_Update("pine-exec-evt-1-protection-0", HELD))

    assert sup._for("BTC/USD") is None, "the lot outlived its position"
    assert sup.store.open_lot_for("BTC/USD") is None


def test_a_fill_for_another_lot_is_ignored(tmp_path):
    broker = _Broker()
    sup = _supervisor(tmp_path, broker)
    lot = sup.adopt(manager.open_lot(_lot(event_id="evt-1"), broker))

    sup.on_order_update(_Update("pine-exec-evt-OTHER-tp1", HELD))

    assert not lot.rung_filled(1)


def test_bars_and_trades_only_reach_the_lot_that_owns_the_symbol(tmp_path):
    broker = _Broker()
    sup = _supervisor(tmp_path, broker)
    lot = sup.adopt(manager.open_lot(_lot(), broker))
    lot.advance_to_runner()

    sup.on_bar(_Bar("QQQ", low=ENTRY + 3 * R))
    assert lot.working_stop == ENTRY, "a QQQ bar moved a BTC stop"

    sup.on_bar(_Bar("BTC/USD", low=ENTRY + 2 * R))
    assert lot.working_stop == ENTRY + 2 * R


# ═══════════════════════════════════════════════════════════ persistence

def test_every_handler_persists_what_it_changed(tmp_path):
    """The window between deciding and writing is the window a crash loses, and
    what it loses is the record of an order that already exists at Alpaca."""
    broker = _Broker()
    store = EventStore(tmp_path / "s.sqlite3")
    sup = supervisor.LotSupervisor(store, broker)
    lot = sup.adopt(manager.open_lot(_lot(), broker))
    lot.on_price(TP1)
    sup.on_order_update(_Update("pine-exec-evt-1-tp1", lot.tranche_qty(1)))

    _event_id, _symbol, state = store.open_lots()[0]
    assert manager.load_lot(state).rung_filled(1), "the fill was never persisted"


def test_startup_re_arms_and_reconciles_before_the_first_tick(tmp_path):
    """A lot whose stop was cancelled while the process was down is unprotected
    now, and the first tick may be a minute away."""
    broker = _Broker()
    store = EventStore(tmp_path / "s.sqlite3")
    lot = _lot()
    lot.stop_order_id, lot.reserved_qty = "ord-vanished", HELD
    store.save_lot(lot.event_id, lot.symbol, lot.stage, manager.dump_lot(lot))

    restored = supervisor.LotSupervisor(store, broker).start()

    assert len(restored) == 1
    stops = [o for o in broker.submitted if o["type"] in ("stop", "stop_limit")]
    assert stops, "startup left a live position with no resting stop"


def test_one_unreadable_row_does_not_strand_the_other_lots(tmp_path):
    """A lot that cannot be rebuilt is a position with no manager. That is bad;
    stopping the loop so the *other* positions also lose theirs is worse."""
    broker = _Broker()
    store = EventStore(tmp_path / "s.sqlite3")
    store.save_lot("broken", "BTC/USD", "ladder", "{not json")
    good = _lot(event_id="evt-2", symbol="QQQ", held_qty=Decimal("10"),
                min_order_size=Decimal("1"))
    store.save_lot(good.event_id, good.symbol, good.stage, manager.dump_lot(good))

    restored = supervisor.LotSupervisor(store, broker).start()

    assert [lot.event_id for lot in restored] == ["evt-2"]


def test_a_closed_lot_stops_receiving_events(tmp_path):
    broker = _Broker()
    sup = _supervisor(tmp_path, broker)
    lot = sup.adopt(manager.open_lot(_lot(), broker))
    lot.advance_to_runner()
    lot.on_bar(high=ENTRY + 3 * R, low=ENTRY + R, close=ENTRY + 3 * R, trade_count=7)
    lot.on_price(ENTRY + R - Decimal("1"))          # trail breached -> closed
    sup.adopt(lot)

    assert sup._for("BTC/USD") is None
    assert sup.store.open_lot_for("BTC/USD") is None


class _Bar:
    def __init__(self, symbol, low, trade_count=11):
        self.symbol, self.low, self.trade_count = symbol, low, trade_count
        self.high = low + Decimal("50")
        self.close = low + Decimal("25")


# ═══════════════════════════════════════════════════ seeding the trail

class _Bar2:
    def __init__(self, low, trade_count=9):
        self.low = low
        self.high = low + Decimal("60")
        self.close = low + Decimal("30")
        self.trade_count = trade_count


def _runner_lot(store, broker):
    lot = manager.open_lot(_lot(), broker)
    lot.advance_to_runner()
    store.save_lot(lot.event_id, lot.symbol, lot.stage, manager.dump_lot(lot))
    return lot


def test_startup_seeds_the_trail_from_recent_bars(tmp_path):
    """Without this a restarted runner trails from its stored stop until a live
    bar arrives — a minute on 1m, and indefinitely if the single market-data
    connection slot is held by another process. The lot looks managed and is
    not."""
    broker = _Broker()
    store = EventStore(tmp_path / "s.sqlite3")
    _runner_lot(store, broker)
    broker.bars = [_Bar2(ENTRY + R), _Bar2(ENTRY + 2 * R)]

    restored = supervisor.LotSupervisor(store, broker).start()

    assert restored[0].working_stop == ENTRY + 2 * R, (
        "the trail was not seeded from history")


def test_seeding_obeys_the_no_trades_rule(tmp_path):
    """Reused rather than reimplemented: a quote-only bar must not seed a stop
    at a price nothing traded at, least of all at startup where nothing will
    correct it before the next bar."""
    broker = _Broker()
    store = EventStore(tmp_path / "s.sqlite3")
    _runner_lot(store, broker)
    broker.bars = [_Bar2(ENTRY + R, trade_count=7),
                   _Bar2(ENTRY + 3 * R, trade_count=0)]

    restored = supervisor.LotSupervisor(store, broker).start()

    assert restored[0].working_stop == ENTRY + R, "a quote-only bar seeded the trail"


def test_seeding_never_lowers_a_stop(tmp_path):
    """History replayed through on_bar keeps the monotonic rule, so an old bar
    cannot walk a stop back down to where it was hours ago."""
    broker = _Broker()
    store = EventStore(tmp_path / "s.sqlite3")
    lot = _runner_lot(store, broker)
    lot.working_stop = ENTRY + 2 * R
    store.save_lot(lot.event_id, lot.symbol, lot.stage, manager.dump_lot(lot))
    broker.bars = [_Bar2(ENTRY)]

    restored = supervisor.LotSupervisor(store, broker).start()

    assert restored[0].working_stop == ENTRY + 2 * R, "seeding loosened the stop"


def test_startup_survives_a_market_data_outage(tmp_path):
    """A lot with a stale trail is worse than one with a fresh trail, and much
    better than a gateway that will not start."""
    broker = _Broker()
    store = EventStore(tmp_path / "s.sqlite3")
    _runner_lot(store, broker)

    def _boom(*a, **k):
        raise RuntimeError("market data unavailable")

    broker.recent_bars = _boom

    restored = supervisor.LotSupervisor(store, broker).start()

    assert len(restored) == 1, "a data outage stopped the gateway re-arming"


def test_a_lot_still_on_the_ladder_is_not_seeded(tmp_path):
    """Before the runner stage the trail is not moving, so replaying bars would
    only cost REST calls."""
    broker = _Broker()
    store = EventStore(tmp_path / "s.sqlite3")
    lot = manager.open_lot(_lot(), broker)
    store.save_lot(lot.event_id, lot.symbol, lot.stage, manager.dump_lot(lot))
    asked = []
    broker.recent_bars = lambda *a, **k: asked.append(a) or []

    supervisor.LotSupervisor(store, broker).start()

    assert not asked, "a laddering lot fetched history it cannot use"


# ════════ the routing key must match what a lot actually calls itself

def test_a_rung_id_resolves_to_the_lots_own_event_id():
    """Live regression, 2026-08-14 16:42:25:

        INFO fill for unmanaged lot btc-tp-ladder-test-3-20260811-1228 (BTC/USD)

    `rung_of` stripped the `pine-exec-` prefix, but a lot's event_id CONTAINS
    it — `_command_id` returns `pine-exec-<identity>` and that is what reaches
    the lot. The comparison could never match.

    It was hidden by a second bug. Client order ids used to double the prefix,
    so stripping one still left the other and the ids matched by accident.
    Fixing the doubling exposed this, and rung fills stopped routing entirely —
    the ladder ran on the 60-second reconcile timer instead. Correct, and a
    minute late.
    """
    from tv_alpaca_gateway.exit_manager import prefixed

    event_id = "pine-exec-btc-tp-ladder-test-3-20260811-1228"
    extracted, rung = supervisor.rung_of(f"{event_id}-tp1")

    assert rung == 1
    assert prefixed(extracted) == prefixed(event_id), (
        "the routing key does not resolve to the lot's own event_id")


def test_the_old_doubled_ids_still_resolve():
    """Lots opened before the prefix fix are still in the store, and a fill for
    one must not be dropped because its id has the older shape."""
    from tv_alpaca_gateway.exit_manager import prefixed

    doubled = "pine-exec-pine-exec-btc-t3-1228-tp1"
    extracted, rung = supervisor.rung_of(doubled)

    assert rung == 1
    assert prefixed(extracted) == prefixed("pine-exec-btc-t3-1228")


def test_a_fill_actually_reaches_the_lot_end_to_end(tmp_path, caplog):
    """The behavioural version, because the unit above passed while routing was
    broken in production — the ids were compared in a different place."""
    import logging

    broker = _Broker()
    sup = _supervisor(tmp_path, broker)
    lot = sup.adopt(manager.open_lot(_lot(event_id="pine-exec-evt-1"), broker))
    lot.on_price(TP1)

    with caplog.at_level(logging.INFO, logger="tv_alpaca_gateway"):
        sup.on_order_update(_Update("pine-exec-evt-1-tp1", lot.tranche_qty(1)))

    assert "unmanaged lot" not in caplog.text, "the fill was dropped as unmanaged"
    assert lot.rung_filled(1), "the fill never reached the lot"


def test_every_parsed_client_order_id_round_trips():
    """The class of bug, not the instance.

    Generating an id and parsing one back are two different code paths, and
    only the parse direction was wrong — which is why everything looked
    consistent right up until a fill arrived. Any id we generate must resolve
    to the lot that generated it.
    """
    from decimal import Decimal

    from tv_alpaca_gateway.exit_manager import prefixed

    # The shapes an event_id actually takes. `_command_id` returns
    # `pine-exec-<identity>`, and a lot created directly in a test may carry a
    # bare id — a DOUBLED event_id is not a real shape: the doubling was in the
    # generated client order id, never in the event_id itself, and asserting on
    # it would be testing an invented case.
    for event_id in ("evt-1", "pine-exec-evt-1",
                     "pine-exec-btc-tp-ladder-test-3-20260811-1228"):
        lot = _lot(event_id=event_id)
        for rung in (1, 2):
            for attempt in (0, 1):
                cid = lot.rung_client_order_id(rung, attempt)
                parsed = supervisor.rung_of(cid)
                assert parsed is not None, f"{cid} did not parse as a rung"
                assert prefixed(parsed[0]) == prefixed(lot.event_id), (
                    f"{cid} resolves to {parsed[0]!r}, not to the lot that made it")
                assert parsed[1] == rung
