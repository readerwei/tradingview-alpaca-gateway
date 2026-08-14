"""A WARNING must mean something, and this one meant "business as usual".

Observed in Wei's 2026-08-14 run — seven of these in one five-minute lot:

    WARNING received update for unknown order_id=9e92ef16-…

Nothing was unknown. The order store records what DIRECT EXECUTION placed —
entry, protection generation 0, flatten — and every order the supervisor places
after the handoff is absent from it by design:

    in the store        NOT in the store
    ──────────────      ─────────────────
    …-entry             …-tp1, …-tp2        rung orders
    …-protection-0      …-protection-1,2…   every replacement stop
    …-flatten           …-stop              the market exit on a breach

So the line fired three times for `tp1`, three for `stop`, once for
`protection-1`. Harmless to state — there is no row to update and there should
not be — and corrosive to read, because Wei runs a SECOND system on this
account. An order placed by that system, or by hand in the Alpaca UI, arrives
on the same socket, is equally absent from the store, and produces a
byte-identical line. The message that should mean

    something is trading your account that this gateway did not place

instead meant "a ladder rung did a normal thing", seven times a lot, until the
eye filters it out entirely. That is how the one that matters gets missed.

The fix is to classify rather than demote: demoting to DEBUG would hide the
noise and the detector together.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

import pytest

from tv_alpaca_gateway.exit_manager import NAMESPACE, is_ours


# ── the predicate ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("client_order_id", [
    "pine-exec-evt-1",                       # entry
    "pine-exec-evt-1-protection-0",          # the disaster stop
    "pine-exec-evt-1-protection-3",          # a replacement after three resizes
    "pine-exec-evt-1-tp1",                   # a rung
    "pine-exec-evt-1-tp2r4",                 # a rung's fifth attempt
    "pine-exec-evt-1-stop",                  # the market exit on a breach
    "pine-exec-evt-1-flatten",
    "pine-exec-evt-1-oco",
])
def test_an_order_this_gateway_placed_is_recognised(client_order_id):
    assert is_ours(client_order_id)


@pytest.mark.parametrize("client_order_id", [
    "",                                      # Alpaca sent none
    None,                                    # the field was absent entirely
    "a1b2c3d4-0000-1111-2222-333344445555",  # placed in the Alpaca UI
    "other-system-tsla-001",                 # Wei's other system
    "pine-exec",                             # the prefix without its trailing dash
    "not-pine-exec-evt-1",                   # namespace in the middle, not the start
])
def test_an_order_this_gateway_did_not_place_is_not_recognised(client_order_id):
    assert not is_ours(client_order_id)


def test_every_id_the_gateway_can_build_starts_from_one_namespace(tmp_path):
    """The property the classifier rests on.

    Every client order id in this codebase is `event_id + suffix`, and
    `_command_id` guarantees `event_id` opens with the namespace. That is what
    makes a prefix test durable where a suffix grammar would not be: a new exit
    reason added next month is classified correctly without anyone remembering
    to update a table.

    Built from the real generators, not from string literals — a test that
    rebuilt the ids itself would agree with itself no matter what shipped.
    """
    from tv_alpaca_gateway import exit_plans
    from tv_alpaca_gateway.exit_manager import Lot, prefixed
    from tv_alpaca_gateway.execution import _command_id
    from tv_alpaca_gateway.pine_alert_parser import parse_pine_alert

    command = parse_pine_alert(
        "EXECUTE_ALPACA_ORDER | SYMBOL=BTC/USD | SIDE=BUY | QTY=0.001 | "
        "ORDER_TYPE=MARKET | TIME_IN_FORCE=GTC | EVENT_ID=evt-1 | "
        "EXIT_PLAN=DYNAMIC_TRAIL | INTERVAL=1 | STOP_TRIGGER=62800 | "
        "STOP_LIMIT=62800 | PLACE_PROTECTIVE_STOP_AFTER_FILL"
    )
    event_id = _command_id(command)
    lot = Lot.opened(
        event_id=event_id, symbol="BTC/USD", entry_price=Decimal("63000"),
        initial_stop=Decimal("62800"), held_qty=Decimal("0.001"),
        timeframe="1m", plan=exit_plans.resolve("DYNAMIC_TRAIL"),
        min_order_size=Decimal("0.000015437"))

    built = [
        event_id,                                       # execution.py: the entry
        f"{lot.stop_client_order_id}-0",                # exit_manager: the stop
        f"{lot.stop_client_order_id}-7",                # …and a later generation
        lot.rung_client_order_id(1),                    # exit_manager: a rung
        lot.rung_client_order_id(2, attempt=3),         # …and a retry
        f"{prefixed(lot.event_id)}-stop",               # exit_manager: breach exit
        f"{event_id}-oco",                              # execution.py
        f"{event_id}-flatten",                          # execution.py
    ]
    for client_order_id in built:
        assert client_order_id.startswith(NAMESPACE), client_order_id
        assert is_ours(client_order_id), client_order_id


# ── the handler ─────────────────────────────────────────────────────────────

class _Update:
    """Shaped like stream.OrderUpdate, only what the handler reads."""

    def __init__(self, client_order_id, order_id="ord-1", symbol="TSLA",
                 side="sell", qty="10", filled_qty="0", event="new",
                 status="new"):
        self.client_order_id = client_order_id
        self.order_id = order_id
        self.symbol = symbol
        self.side = side
        self.qty = qty
        self.filled_qty = filled_qty
        self.event = event
        self.status = status
        self.raw = {}

    @property
    def is_fill(self):
        return self.event == "fill"


def _handler(tmp_path):
    """The handler the trade-updates socket will actually call.

    Read back off the stream object rather than rebuilt here: asserting on a
    closure the app never wired is the same mistake one level up, and this
    suite has made it before.
    """
    from tv_alpaca_gateway.app import create_app
    from tv_alpaca_gateway.broker import FakeBroker
    from tv_alpaca_gateway.config import Settings
    from tv_alpaca_gateway.store import EventStore

    settings = Settings(
        paper_trading=True, trading_enabled=True, webhook_secret="s",
        allowed_symbols=frozenset({"TSLA", "BTC/USD"}), max_qty=50,
        max_notional=100_000.0, db_path=tmp_path / "foreign.sqlite3",
        stream_enabled=True, alpaca_key_id="PK-test", alpaca_secret_key="s",
    )
    app = create_app(settings, FakeBroker(), EventStore(settings.db_path))
    return app.state.stream.trade_updates.on_update


@pytest.mark.parametrize("client_order_id", [
    "pine-exec-evt-1-tp1",
    "pine-exec-evt-1-tp2r1",
    "pine-exec-evt-1-protection-1",
    "pine-exec-evt-1-stop",
])
def test_a_supervisor_owned_order_does_not_warn(tmp_path, caplog, client_order_id):
    """The seven lines from Wei's run. All four shapes, none of them a warning."""
    handler = _handler(tmp_path)
    with caplog.at_level(logging.DEBUG, logger="tv_alpaca_gateway"):
        asyncio.run(handler(_Update(client_order_id)))

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings, f"a routine ladder order warned: {[r.message for r in warnings]}"


def test_an_order_the_gateway_never_placed_warns(tmp_path, caplog):
    """The signal the noise was burying. Wei runs another system on this
    account; this is the line that says so."""
    handler = _handler(tmp_path)
    with caplog.at_level(logging.DEBUG, logger="tv_alpaca_gateway"):
        asyncio.run(handler(_Update("other-system-tsla-001", order_id="ord-9")))

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, "a foreign order on the account did not warn"


def test_the_foreign_warning_says_what_the_order_was(tmp_path, caplog):
    """The old line printed an order_id and nothing else, so even a true
    positive told you nothing — you could not tell what symbol or size without
    going to Alpaca. A warning you have to research is a warning you postpone."""
    handler = _handler(tmp_path)
    with caplog.at_level(logging.DEBUG, logger="tv_alpaca_gateway"):
        asyncio.run(handler(_Update("other-system-tsla-001", symbol="NVDA",
                                    side="buy", qty="250")))

    warning = [r for r in caplog.records if r.levelno >= logging.WARNING][0]
    for field in ("NVDA", "buy", "250"):
        assert field in warning.getMessage(), f"the warning does not name {field}"


def test_one_foreign_order_warns_once_however_many_events_it_sends(tmp_path, caplog):
    """Otherwise the fix becomes the next flood.

    One order emits new -> partial_fill -> fill, which is exactly why a single
    `tp1` tripped the old warning three times. Wei's other system is active on
    this account, so per-event warning would bury the detector again — this
    time in true positives, which is harder to notice.
    """
    handler = _handler(tmp_path)
    with caplog.at_level(logging.DEBUG, logger="tv_alpaca_gateway"):
        for event, filled in (("new", "0"), ("partial_fill", "100"), ("fill", "250")):
            asyncio.run(handler(_Update("other-system-tsla-001", order_id="ord-9",
                                        event=event, filled_qty=filled)))

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, f"one order warned {len(warnings)} times"


def test_two_different_foreign_orders_each_warn(tmp_path, caplog):
    """The dedupe must key on the order, not silence the second one."""
    handler = _handler(tmp_path)
    with caplog.at_level(logging.DEBUG, logger="tv_alpaca_gateway"):
        asyncio.run(handler(_Update("other-a", order_id="ord-A")))
        asyncio.run(handler(_Update("other-b", order_id="ord-B")))

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 2, "the second foreign order was swallowed by the dedupe"


def test_a_supervisor_owned_fill_still_reaches_the_lot(tmp_path):
    """Classification is a logging change. If it altered routing it would be
    re-breaking #58, which is the bug this run was diagnosing in the first place.
    """
    import inspect

    from tv_alpaca_gateway.app import create_app

    source = inspect.getsource(create_app)
    classifying = source.index("is_ours")
    routing = source.index("supervisor.on_order_update")
    assert classifying < routing, "classification moved below the routing call"
    assert "return" not in source[classifying:routing], (
        "the classifier can return before the fill is routed")
