from datetime import datetime, timezone

from fastapi.testclient import TestClient

from tv_alpaca_gateway.app import create_app
from tv_alpaca_gateway.broker import FakeBroker
from tv_alpaca_gateway.config import Settings
from tv_alpaca_gateway.reconcile import reconcile
from tv_alpaca_gateway.store import EventStore


def payload(event_id="evt-1"):
    return {
        "event_id": event_id,
        "symbol": "QQQ",
        "action": "buy",
        "timeframe": "1m",
        "bar_time": datetime.now(timezone.utc).isoformat(),
        "close": 700,
    }


def make_app(tmp_path, enabled=False):
    settings = Settings(
        trading_enabled=enabled,
        webhook_secret="secret",
        db_path=tmp_path / "events.sqlite3",
        allowed_symbols=frozenset({"QQQ"}),
    )
    broker = FakeBroker()
    app = create_app(settings, broker=broker, store=EventStore(settings.db_path))
    return TestClient(app), broker


def test_health_is_paper_only_and_kill_switch_defaults_off(tmp_path):
    client, _ = make_app(tmp_path)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "paper_trading": True, "trading_enabled": False}


def test_invalid_secret_is_rejected(tmp_path):
    client, _ = make_app(tmp_path)
    response = client.post("/webhooks/tradingview", json=payload(), headers={"x-tv-secret": "wrong"})
    assert response.status_code == 401


def test_kill_switch_claims_but_does_not_submit(tmp_path):
    client, broker = make_app(tmp_path, enabled=False)
    response = client.post("/webhooks/tradingview", json=payload(), headers={"x-tv-secret": "secret"})
    assert response.status_code == 200
    assert response.json()["reason"] == "kill_switch"
    assert broker.orders == []


def test_duplicate_event_is_not_submitted_twice(tmp_path):
    client, broker = make_app(tmp_path, enabled=True)
    headers = {"x-tv-secret": "secret"}
    first = client.post("/webhooks/tradingview", json=payload("same"), headers=headers)
    second = client.post("/webhooks/tradingview", json=payload("same"), headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert len(broker.orders) == 1


def test_symbol_allowlist_rejects_unapproved_symbol(tmp_path):
    client, broker = make_app(tmp_path, enabled=True)
    body = payload()
    body["symbol"] = "AAPL"
    response = client.post("/webhooks/tradingview", json=body, headers={"x-tv-secret": "secret"})
    assert response.status_code == 403
    assert broker.orders == []




def test_order_reconciliation_reads_broker_state(tmp_path):
    client, broker = make_app(tmp_path, enabled=True)
    response = client.post("/webhooks/tradingview", json=payload("reconcile-me"), headers={"x-tv-secret": "secret"})
    order_id = response.json()["order_id"]
    result = reconcile("reconcile-me", order_id, broker, EventStore(tmp_path / "events.sqlite3"))
    assert result.status == "accepted"
    assert result.order_id == order_id


PINE_ALERT = (
    "EXECUTE_ALPACA_ORDER | SYMBOL=BTCUSD | SIDE=BUY | QTY=0.001 | "
    "ORDER_TYPE=MARKET | TIME_IN_FORCE=GTC | "
    "CANCEL_UNFILLED_AT_DEADLINE=YES | PLACE_PROTECTIVE_STOP_AFTER_FILL | "
    "STOP_TRIGGER=65000 | STOP_LIMIT=64950 | TRAIL=NONE | "
    "REQUIRED_ACTIONS=SUBMIT_ORDER | DO_NOT_SUMMARIZE_OR_REPOST_BEFORE_BROKER_CALL"
)


def test_pine_dry_run_parses_and_audits_without_broker_submission(tmp_path):
    client, broker = make_app(tmp_path, enabled=True)
    response = client.post(
        "/webhooks/tradingview/pine/dry-run",
        content=PINE_ALERT,
        headers={"x-tv-secret": "secret", "content-type": "text/plain"},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["dry_run"] is True
    assert result["command"] == {
        "symbol": "BTC/USD",
        "side": "buy",
        "qty": "0.001",
        "order_type": "market",
        "time_in_force": "gtc",
        "cancel_unfilled_at_deadline": True,
        "place_protective_stop_after_fill": True,
        "stop_trigger": "65000",
        "stop_limit": "64950",
        "trail": None,
    }
    assert client.app.state.store.pine_dry_run_status(result["audit_id"]) == "pine_dry_run"
    assert broker.orders == []


def test_pine_dry_run_rejects_invalid_alert_without_broker_submission(tmp_path):
    client, broker = make_app(tmp_path, enabled=True)
    response = client.post(
        "/webhooks/tradingview/pine/dry-run",
        content=PINE_ALERT.replace("QTY=0.001", "QTY=0"),
        headers={"x-tv-secret": "secret", "content-type": "text/plain"},
    )

    assert response.status_code == 422
    assert broker.orders == []


def test_pine_dry_run_requires_the_webhook_secret(tmp_path):
    client, broker = make_app(tmp_path, enabled=True)
    response = client.post(
        "/webhooks/tradingview/pine/dry-run", content=PINE_ALERT,
        headers={"x-tv-secret": "wrong", "content-type": "text/plain"},
    )

    assert response.status_code == 401
    assert broker.orders == []


def test_pine_dry_run_rejects_oversized_body_without_broker_submission(tmp_path):
    client, broker = make_app(tmp_path, enabled=True)
    response = client.post(
        "/webhooks/tradingview/pine/dry-run", content=b"x" * 4097,
        headers={"x-tv-secret": "secret", "content-type": "text/plain"},
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "Pine alert exceeds 4096 byte limit"
    assert broker.orders == []


def test_pine_dry_run_cannot_block_an_executable_event_id(tmp_path):
    client, broker = make_app(tmp_path, enabled=True)
    dry_run = client.post(
        "/webhooks/tradingview/pine/dry-run", content=PINE_ALERT,
        headers={"x-tv-secret": "secret", "content-type": "text/plain"},
    )
    assert dry_run.status_code == 200

    executable = client.post(
        "/webhooks/tradingview", json=payload(dry_run.json()["audit_id"]),
        headers={"x-tv-secret": "secret"},
    )
    assert executable.status_code == 200
    assert executable.json()["executed"] is True
    assert len(broker.orders) == 1


def test_pine_dry_run_never_looks_up_or_submits_to_broker(tmp_path):
    client, broker = make_app(tmp_path, enabled=True)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("dry run reached broker")

    broker.latest_trade_price = forbidden
    broker.submit = forbidden
    response = client.post(
        "/webhooks/tradingview/pine/dry-run", content=PINE_ALERT,
        headers={"x-tv-secret": "secret", "content-type": "text/plain"},
    )

    assert response.status_code == 200


def test_the_dry_run_states_that_it_only_parsed(tmp_path):
    """"dry_run": true reads as "this is what would happen if I sent it".

    It means "this parsed". With an unlisted symbol, no configured crypto size,
    a notional over the cap and the kill switch on, the endpoint still returns
    200 — four independent risk refusals behind one green response. Naming what
    was not checked is what stops a parse being read as an approval.
    """
    from decimal import Decimal

    from fastapi.testclient import TestClient

    from tv_alpaca_gateway.app import create_app
    from tv_alpaca_gateway.config import Settings
    from tv_alpaca_gateway.store import EventStore

    secret = "s3cret"
    settings = Settings(
        paper_trading=True, trading_enabled=False, webhook_secret=secret,
        allowed_symbols=frozenset({"QQQ"}), crypto_max_qty=Decimal("0"),
        max_notional=10.0, db_path=tmp_path / "g.sqlite3",
    )
    client = TestClient(create_app(settings, None, EventStore(settings.db_path), None),
                        raise_server_exceptions=False)
    alert = ("EXECUTE_ALPACA_ORDER | SYMBOL=BTCUSD | SIDE=BUY | QTY=0.001 | "
             "ORDER_TYPE=MARKET | TIME_IN_FORCE=GTC")

    body = client.post("/webhooks/tradingview/pine/dry-run", content=alert,
                       headers={"x-tv-secret": secret}).json()

    assert body["validated"] == "parse_only"
    # Each of these would have refused the order; none was consulted.
    for skipped in ("allowlist", "sizing", "notional", "kill_switch"):
        assert skipped in body["not_checked"]


def test_the_not_checked_list_is_true_by_behaviour_not_by_comment(tmp_path):
    """The list is a claim about what the route skips. Assert it behaviourally.

    A broker whose every method raises proves the risk and price paths were not
    taken: if the route consulted them the request would fail instead of
    returning 200. Inspecting the source for `approve(` would also "pass" and
    would break the moment the code is reformatted — the point is the behaviour,
    not the spelling.
    """
    from decimal import Decimal

    from fastapi.testclient import TestClient

    from tv_alpaca_gateway.app import _DRY_RUN_NOT_CHECKED, create_app
    from tv_alpaca_gateway.config import Settings
    from tv_alpaca_gateway.store import EventStore

    class ExplodingBroker:
        def submit(self, *a, **k):
            raise AssertionError("dry-run reached the broker")

        def get_order(self, *a, **k):
            raise AssertionError("dry-run reached the broker")

        def latest_trade_price(self, *a, **k):
            raise AssertionError("dry-run reached market data")

    secret = "s3cret"
    settings = Settings(
        paper_trading=True, trading_enabled=True, webhook_secret=secret,
        allowed_symbols=frozenset({"QQQ"}), crypto_max_qty=Decimal("0"),
        max_notional=1.0, db_path=tmp_path / "g.sqlite3",
    )
    client = TestClient(
        create_app(settings, ExplodingBroker(), EventStore(settings.db_path), None),
        raise_server_exceptions=False)

    response = client.post(
        "/webhooks/tradingview/pine/dry-run",
        content=("EXECUTE_ALPACA_ORDER | SYMBOL=BTCUSD | SIDE=BUY | QTY=99 | "
                 "ORDER_TYPE=MARKET | TIME_IN_FORCE=GTC"),
        headers={"x-tv-secret": secret})

    assert response.status_code == 200, "the risk or market-data path was taken"
    assert response.json()["validated"] == "parse_only"
    assert _DRY_RUN_NOT_CHECKED, "the list must not be empty while risk is skipped"
