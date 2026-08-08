from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tv_alpaca_gateway.pine_alert_parser import AlertParseError, parse_pine_alert


def _now() -> str:
    """BAR_TIME is generated per call, never hardcoded.

    A fixed timestamp passes the freshness rule when written and starts failing
    it a few minutes later — a decayed fixture that looks exactly like a parser
    regression. Tests that pass in the morning and fail after lunch are worse
    than tests that never passed.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


ALERT = (
    "EXECUTE_ALPACA_ORDER | SYMBOL=BTCUSD | SIDE=BUY | QTY=0.001 | "
    f"EVENT_ID=BTCUSD-1-fixture | BAR_TIME={_now()} | "
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


def test_allows_documented_non_executable_instruction_fields():
    command = parse_pine_alert(
        ALERT + " | REQUIRED_ACTIONS=IGNORE_THIS_TOO"
        " | DO_NOT_SUMMARIZE_OR_REPOST_BEFORE_BROKER_CALL"
    )

    assert command.symbol == "BTC/USD"
    assert command.stop_limit == Decimal("64950")


def test_accepts_leading_transport_decoration_before_unique_prefix():
    command = parse_pine_alert("<@12345> " + ALERT)

    assert command.symbol == "BTC/USD"


@pytest.mark.parametrize(
    ("alert", "message"),
    [
        (ALERT.replace("TRAIL=NONE", "TRAIL=250"), "TRAIL is unsupported for crypto"),
        (ALERT.replace("TIME_IN_FORCE=GTC", "TIME_IN_FORCE=DAY"),
         "TIME_IN_FORCE is not supported"),
    ],
)
def test_rejects_crypto_incompatible_controls_at_parse_time(alert, message):
    with pytest.raises(AlertParseError, match=message):
        parse_pine_alert(alert)


@pytest.mark.parametrize(
    "field",
    ["UNKNOWN_FUTURE_FIELD=value", "TRIAL=250", "STOP_TRIGER=65000"],
)
def test_rejects_unrecognised_fields(field):
    with pytest.raises(AlertParseError, match="unrecognised field"):
        parse_pine_alert(ALERT + " | " + field)


def test_rejects_multiple_execution_prefixes():
    with pytest.raises(AlertParseError, match="exactly one"):
        parse_pine_alert(ALERT + " | EXECUTE_ALPACA_ORDER")


@pytest.mark.parametrize("prefix", ["DONT_EXECUTE_ALPACA_ORDER", "NEVER_EXECUTE_ALPACA_ORDER"])
def test_rejects_prefix_embedded_in_non_execution_text(prefix):
    alert = ALERT.replace("EXECUTE_ALPACA_ORDER", prefix, 1)

    with pytest.raises(AlertParseError, match="execution prefix"):
        parse_pine_alert(alert)


def test_applies_crypto_rules_to_any_slash_form_pair():
    alert = ALERT.replace("SYMBOL=BTCUSD", "SYMBOL=SOL/USD")

    with pytest.raises(AlertParseError, match="TRAIL is unsupported for crypto"):
        parse_pine_alert(alert.replace("TRAIL=NONE", "TRAIL=250"))

    with pytest.raises(AlertParseError, match="TIME_IN_FORCE is not supported"):
        parse_pine_alert(alert.replace("TIME_IN_FORCE=GTC", "TIME_IN_FORCE=DAY"))


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
