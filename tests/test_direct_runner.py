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


# ════════════════════ --once cannot manage what it will not stay alive for

def _plan_alert(plan="DYNAMIC_TRAIL"):
    return ("EXECUTE_ALPACA_ORDER | SYMBOL=BTC/USD | SIDE=BUY | QTY=0.0015 | "
            "ORDER_TYPE=MARKET | TIME_IN_FORCE=GTC | "
            "PLACE_PROTECTIVE_STOP_AFTER_FILL | STOP_TRIGGER=64000 | "
            f"STOP_LIMIT=63900 | EXIT_PLAN={plan} | INTERVAL=1")


def test_once_refuses_a_managed_exit_plan(tmp_path, capsys):
    """The flag that cost two days.

    `--once` returns as soon as the entry fills, closing the lifespan and
    taking the supervisor, the sockets and the reconcile timer with it. A
    managed plan then arms its disaster stop, writes its lot, and has nothing
    left listening — so no rung ever fires, and every symptom points at the
    ladder rather than at the flag.
    """
    from tv_alpaca_gateway import direct_runner

    alert = tmp_path / "a.txt"
    alert.write_text(_plan_alert())

    code = direct_runner.main(["--alert-file", str(alert), "--execute", "--once"])

    assert code != 0, "--once accepted a plan it cannot manage"
    err = capsys.readouterr().err
    assert "EXIT_PLAN" in err and "--once" in err
    assert "supervisor" in err.lower(), "the reason should name what is lost"


def test_once_is_still_fine_without_an_exit_plan(tmp_path):
    """Fire-and-forget is what the flag is for; this must not become an
    outage for the ordinary protective-stop path."""
    from tv_alpaca_gateway import direct_runner

    alert = tmp_path / "a.txt"
    alert.write_text(_plan_alert().split(" | EXIT_PLAN")[0])

    # --execute omitted: parsing and admission only, no broker contact.
    assert direct_runner.main(["--alert-file", str(alert)]) == 0


def test_a_managed_plan_without_once_is_admitted(tmp_path):
    """The guard must refuse the combination, not the plan."""
    from tv_alpaca_gateway import direct_runner

    alert = tmp_path / "a.txt"
    alert.write_text(_plan_alert())

    assert direct_runner.main(["--alert-file", str(alert)]) == 0
