from decimal import Decimal

from tv_alpaca_gateway.order_manager import ExitManager, ExitPlan, FillEvent


class FakeOrderBroker:
    def __init__(self):
        self.calls = []

    def submit_limit(self, symbol, side, qty, limit_price, client_order_id):
        self.calls.append(("submit_limit", symbol, side, qty, limit_price, client_order_id))
        return f"tp-{len([c for c in self.calls if c[0] == 'submit_limit'])}"

    def submit_trailing_stop(self, symbol, side, qty, trail_percent, client_order_id):
        self.calls.append(("submit_trailing_stop", symbol, side, qty, trail_percent, client_order_id))
        return "trail-1"

    def cancel(self, order_id):
        self.calls.append(("cancel", order_id))

    def replace_qty(self, order_id, qty):
        self.calls.append(("replace_qty", order_id, qty))


def test_start_creates_split_take_profits_and_one_trailing_stop():
    broker = FakeOrderBroker()
    manager = ExitManager(
        broker,
        ExitPlan(
            symbol="QQQ",
            take_profits=((Decimal("725"), 3), (Decimal("730"), 3)),
            trail_percent=Decimal("2"),
        ),
    )

    state = manager.start(position_qty=10)

    assert state.remaining_qty == 10
    assert [call[0] for call in broker.calls] == [
        "submit_limit",
        "submit_limit",
        "submit_trailing_stop",
    ]
    assert broker.calls[0][3:5] == (3, Decimal("725"))
    assert broker.calls[1][3:5] == (3, Decimal("730"))
    assert broker.calls[2][3:5] == (4, Decimal("2"))


def test_take_profit_fill_cancels_trailing_stop_and_reduces_remaining_quantity():
    broker = FakeOrderBroker()
    manager = ExitManager(
        broker,
        ExitPlan(
            symbol="QQQ",
            take_profits=((Decimal("725"), 3), (Decimal("730"), 3)),
            trail_percent=Decimal("2"),
        ),
    )
    state = manager.start(10)

    state = manager.on_fill(FillEvent(order_id=state.take_profit_order_ids[0], filled_qty=3))

    assert state.remaining_qty == 7
    assert ("replace_qty", "trail-1", 7) in broker.calls


def test_trailing_stop_fill_cancels_unfilled_take_profits():
    broker = FakeOrderBroker()
    manager = ExitManager(
        broker,
        ExitPlan(
            symbol="QQQ",
            take_profits=((Decimal("725"), 3), (Decimal("730"), 3)),
            trail_percent=Decimal("2"),
        ),
    )
    state = manager.start(10)

    state = manager.on_fill(FillEvent(order_id=state.trailing_stop_order_id, filled_qty=4))

    assert state.remaining_qty == 6
    assert ("cancel", state.take_profit_order_ids[0]) in broker.calls
    assert ("cancel", state.take_profit_order_ids[1]) in broker.calls
