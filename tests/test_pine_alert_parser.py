from decimal import Decimal

import pytest

from tv_alpaca_gateway.pine_alert_parser import AlertParseError, parse_pine_alert


ALERT = (
    "EXECUTE_ALPACA_ORDER | SYMBOL=BTCUSD | SIDE=BUY | QTY=0.001 | "
    "ORDER_TYPE=MARKET | TIME_IN_FORCE=GTC | "
    "CANCEL_UNFILLED_AT_DEADLINE=YES | PLACE_PROTECTIVE_STOP_AFTER_FILL | "
    "STOP_TRIGGER=65000 | STOP_LIMIT=64950 | TRAIL=NONE | "
    "REQUIRED_ACTIONS=SUBMIT_ORDER,VERIFY_BROKER_ORDER_ID,VERIFY_FILL_STATUS,"
    "MONITOR_UNTIL_60S_DEADLINE,CANCEL_REMAINDER,REPORT_FINAL_BROKER_STATUS | "
    "DO_NOT_SUMMARIZE_OR_REPOST_BEFORE_BROKER_CALL"
)


def test_parses_relevant_fields_from_current_pine_alert():
    command = parse_pine_alert(ALERT)

    assert command.symbol == "BTC/USD"
    assert command.side == "buy"
    assert command.qty == Decimal("0.001")
    assert command.order_type == "market"
    assert command.time_in_force == "gtc"
    assert command.cancel_unfilled_at_deadline is True
    assert command.place_protective_stop_after_fill is True
    assert command.stop_trigger == Decimal("65000")
    assert command.stop_limit == Decimal("64950")
    assert command.trail is None


def test_ignores_unrelated_instruction_fields():
    command = parse_pine_alert(
        ALERT + " | REQUIRED_ACTIONS=IGNORE_THIS_TOO | UNKNOWN_FUTURE_FIELD=value"
    )

    assert command.symbol == "BTC/USD"
    assert command.stop_limit == Decimal("64950")


@pytest.mark.parametrize(
    ("alert", "message"),
    [
        (ALERT + " | QTY=0.002", "duplicate executable field: QTY"),
        (ALERT + " | side=SELL", "duplicate executable field: SIDE"),
        (ALERT.replace("CANCEL_UNFILLED_AT_DEADLINE=YES", "CANCEL_UNFILLED_AT_DEADLINE=MAYBE"),
         "CANCEL_UNFILLED_AT_DEADLINE must be YES or NO"),
        (ALERT.replace("STOP_LIMIT=64950", "STOP_LIMIT=65001"),
         "protective BUY stop-limit requires STOP_LIMIT <= STOP_TRIGGER"),
        (ALERT + " | PLACE_PROTECTIVE_STOP_AFTER_FILL",
         "duplicate executable flag: PLACE_PROTECTIVE_STOP_AFTER_FILL"),
        (ALERT.replace("ORDER_TYPE=MARKET", "ORDER_TYPE=LIMIT"),
         "ORDER_TYPE must be MARKET"),
        (ALERT.replace("ORDER_TYPE=MARKET", "ORDER_TYPE=STOP_LIMIT"),
         "ORDER_TYPE must be MARKET"),
        (ALERT.replace("SIDE=BUY", "SIDE=SELL").replace("STOP_LIMIT=64950", "STOP_LIMIT=64950"),
         "protective SELL stop-limit requires STOP_LIMIT >= STOP_TRIGGER"),
    ],
)
def test_rejects_ambiguous_or_unsafe_executable_fields(alert, message):
    with pytest.raises(AlertParseError, match=message):
        parse_pine_alert(alert)


@pytest.mark.parametrize(
    ("alert", "message"),
    [
        (ALERT.replace("EXECUTE_ALPACA_ORDER", "BUY", 1), "execution prefix"),
        (ALERT.replace("QTY=0.001", "QTY=0"), "qty must be positive"),
        (ALERT.replace("TRAIL=NONE", "TRAIL=-1"), "trail must be positive"),
        (ALERT.replace("STOP_LIMIT=64950", ""), "STOP_LIMIT"),
    ],
)
def test_rejects_invalid_or_incomplete_relevant_fields(alert, message):
    with pytest.raises(AlertParseError, match=message):
        parse_pine_alert(alert)
