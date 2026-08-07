from decimal import Decimal

from tv_alpaca_gateway.alpaca_exit_broker import AlpacaPaperExitBroker
from tv_alpaca_gateway.config import Settings


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
