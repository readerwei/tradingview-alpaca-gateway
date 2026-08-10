from decimal import Decimal

from tv_alpaca_gateway.alpaca_exit_broker import AlpacaPaperExitBroker
from tv_alpaca_gateway.config import Settings
from tv_alpaca_gateway.broker import AlpacaPaperClient


class CapturingBroker(AlpacaPaperExitBroker):
    def __init__(self):
        self.requests = []

    def _request(self, method, path, payload=None):
        self.requests.append((method, path, payload))
        return {"id": f"id-{len(self.requests)}"}


def test_paper_exit_broker_uses_limit_and_trailing_stop_payloads():
    broker = CapturingBroker()

    assert broker.submit_limit("QQQ", "sell", 3, Decimal("725"), "tp-1") == "id-1"
    assert broker.submit_trailing_stop("QQQ", "sell", 4, Decimal("2"), "trail-1") == "id-2"

    assert broker.requests == [
        (
            "POST",
            "/v2/orders",
            {
                "symbol": "QQQ",
                "side": "sell",
                "qty": "3",
                "type": "limit",
                "time_in_force": "gtc",
                "limit_price": "725",
                "client_order_id": "tp-1",
            },
        ),
        (
            "POST",
            "/v2/orders",
            {
                "symbol": "QQQ",
                "side": "sell",
                "qty": "4",
                "type": "trailing_stop",
                "time_in_force": "gtc",
                "trail_percent": "2",
                "client_order_id": "trail-1",
            },
        ),
    ]


def test_alpaca_client_builds_native_oco_request_with_optional_stop_limit():
    client = object.__new__(AlpacaPaperClient)
    request = client._order_request(
        symbol="QQQ", qty=302, side="sell", type="limit", time_in_force="gtc",
        order_class="oco", take_profit_limit_price=724.89,
        stop_loss_stop_price=723.65, stop_loss_limit_price=None,
        client_order_id="event-oco")

    assert request.order_class.value == "oco"
    assert request.take_profit.limit_price == 724.89
    assert request.stop_loss.stop_price == 723.65
    assert request.stop_loss.limit_price is None
