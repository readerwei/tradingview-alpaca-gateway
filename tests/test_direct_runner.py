from decimal import Decimal

from tv_alpaca_gateway.direct_runner import build_parser, command_payload
from tv_alpaca_gateway.pine_alert_parser import parse_pine_alert


ALERT = (
    "EXECUTE_ALPACA_ORDER | SYMBOL=QQQ | SIDE=BUY | QTY=1 | "
    "ORDER_TYPE=MARKET | TIME_IN_FORCE=DAY | EVENT_ID=direct-1 | "
    "EXIT_PLAN=OCO_AFTER_FILL | STOP_TRIGGER=700 | STOP_LIMIT=NONE | "
    "TAKE_PROFIT=740"
)


def test_direct_runner_requires_explicit_execute_flag():
    parser = build_parser()
    assert parser.parse_args(["--alert-file", "alert.txt"]).execute is False
    assert parser.parse_args(["--alert-file", "alert.txt", "--execute"]).execute is True


def test_direct_runner_serializes_the_same_parsed_command_without_secrets():
    command = parse_pine_alert(ALERT)
    payload = command_payload(command)
    assert payload["symbol"] == "QQQ"
    assert payload["exit_plan"] == "OCO_AFTER_FILL"
    assert payload["stop_trigger"] == "700"
    assert payload["take_profit"] == "740"
    assert all("secret" not in key.lower() for key in payload)
