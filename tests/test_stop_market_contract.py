from decimal import Decimal

import pytest

from tv_alpaca_gateway.execution import _protection_kwargs
from tv_alpaca_gateway.pine_alert_parser import AlertParseError, parse_pine_alert


EQUITY_MANAGED_STOP_MARKET = (
    "EXECUTE_ALPACA_ORDER | SYMBOL=QQQ | SIDE=BUY | QTY=1 | "
    "ORDER_TYPE=MARKET | TIME_IN_FORCE=DAY | EXIT_PLAN=DYNAMIC_TRAIL | "
    "INTERVAL=1m | STOP_TRIGGER=700 | STOP_LIMIT=NONE"
)

CRYPTO_MANAGED_STOP_MARKET = (
    "EXECUTE_ALPACA_ORDER | SYMBOL=BTC/USD | SIDE=BUY | QTY=0.001 | "
    "ORDER_TYPE=MARKET | TIME_IN_FORCE=GTC | EXIT_PLAN=DYNAMIC_TRAIL | "
    "INTERVAL=1m | STOP_TRIGGER=70000 | STOP_LIMIT=NONE"
)


def test_equity_managed_none_stop_limit_means_stop_market():
    command = parse_pine_alert(EQUITY_MANAGED_STOP_MARKET)

    assert command.stop_limit is None
    kwargs = _protection_kwargs(command, "QQQ", False, Decimal("1"), "evt")

    assert kwargs["type"] == "stop"
    assert kwargs["stop_price"] == Decimal("700")
    assert "limit_price" not in kwargs


def test_crypto_managed_none_stop_limit_is_rejected_before_submission():
    with pytest.raises(AlertParseError, match="crypto protection requires"):
        parse_pine_alert(CRYPTO_MANAGED_STOP_MARKET)
