from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from tv_alpaca_gateway.app import create_app
from tv_alpaca_gateway.broker import AlpacaPaperClient, BrokerResult, FakeBroker
from tv_alpaca_gateway.config import Settings
from tv_alpaca_gateway.risk import ApprovedOrder
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


def test_create_app_validates_paper_only_settings_before_injected_broker(tmp_path):
    settings = Settings(
        paper_trading=False,
        webhook_secret="secret",
        db_path=tmp_path / "unsafe.sqlite3",
    )
    broker = FakeBroker()

    with pytest.raises(ValueError, match="paper-only"):
        create_app(settings, broker=broker, store=EventStore(settings.db_path))


def test_create_app_rejects_non_paper_url_before_injected_broker(tmp_path):
    settings = Settings(
        alpaca_base_url="https://api.alpaca.markets",
        webhook_secret="secret",
        db_path=tmp_path / "unsafe-url.sqlite3",
    )

    with pytest.raises(ValueError, match="paper API URL"):
        create_app(settings, broker=FakeBroker(), store=EventStore(settings.db_path))


def test_create_app_rejects_injected_broker_without_paper_only_capability(tmp_path):
    settings = Settings(
        trading_enabled=True,
        webhook_secret="secret",
        db_path=tmp_path / "unsafe-broker.sqlite3",
    )

    class UnsafeBroker:
        def submit(self, *_args, **_kwargs):
            raise AssertionError("unsafe broker must not be accepted")

    with pytest.raises(ValueError, match="paper-only broker"):
        create_app(settings, broker=UnsafeBroker(), store=EventStore(settings.db_path))


def test_executable_routes_recheck_paper_invariant_after_app_creation(tmp_path):
    settings = Settings(
        trading_enabled=True,
        webhook_secret="secret",
        db_path=tmp_path / "mutated-settings.sqlite3",
    )
    broker = FakeBroker()
    client = TestClient(create_app(settings, broker=broker, store=EventStore(settings.db_path)))
    object.__setattr__(settings, "paper_trading", False)

    response = client.post("/webhooks/tradingview", json=payload(), headers={"x-tv-secret": "secret"})

    assert response.status_code == 503
    assert response.json()["detail"] == "paper-only execution invariant is not satisfied"
    assert broker.orders == []


def test_alpaca_paper_client_rechecks_paper_invariant_at_submission_boundary(tmp_path, monkeypatch):
    settings = Settings(
        trading_enabled=True,
        webhook_secret="secret",
        db_path=tmp_path / "client-boundary.sqlite3",
        alpaca_key_id="paper-key",
        alpaca_secret_key="paper-secret",
    )
    client = AlpacaPaperClient(settings)
    object.__setattr__(settings, "paper_trading", False)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("unsafe client attempted a broker request")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)

    with pytest.raises(ValueError, match="paper-only"):
        client.submit(
            ApprovedOrder("QQQ", "buy", Decimal("1"), 700.0),
            "client-boundary-order",
        )


def test_invalid_secret_is_rejected(tmp_path):
    client, _ = make_app(tmp_path)
    response = client.post("/webhooks/tradingview", json=payload(), headers={"x-tv-secret": "wrong"})
    assert response.status_code == 401


def test_legacy_executable_route_caps_body_after_authentication(tmp_path):
    client, broker = make_app(tmp_path, enabled=True)
    response = client.post(
        "/webhooks/tradingview",
        content=b"x" * (4096 + 1),
        headers={"x-tv-secret": "secret", "content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "Pine alert exceeds 4096 byte limit"
    assert broker.orders == []


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


def test_pine_preview_applies_server_risk_without_submitting(tmp_path):
    settings = Settings(
        trading_enabled=True,
        webhook_secret="secret",
        db_path=tmp_path / "preview-events.sqlite3",
        allowed_symbols=frozenset({"BTC/USD"}),
        crypto_max_qty=Decimal("0.001"),
        max_notional=1000.0,
    )
    broker = FakeBroker()
    client = TestClient(create_app(settings, broker=broker, store=EventStore(settings.db_path)))
    response = client.post(
        "/webhooks/tradingview/pine/preview",
        content=PINE_ALERT.replace(
            "CANCEL_UNFILLED_AT_DEADLINE=YES | PLACE_PROTECTIVE_STOP_AFTER_FILL | "
            "STOP_TRIGGER=65000 | STOP_LIMIT=64950 | ",
            "",
        ),
        headers={"x-tv-secret": "secret", "content-type": "text/plain"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "preview": True,
        "approved": True,
        "symbol": "BTC/USD",
        "side": "buy",
        "qty": "0.001",
        "order_type": "market",
        "timeframe": "1m",
    }
    assert broker.orders == []


def test_pine_preview_rejects_lifecycle_and_protection_controls_until_phase_four(tmp_path):
    settings = Settings(
        trading_enabled=True,
        webhook_secret="secret",
        db_path=tmp_path / "preview-controls.sqlite3",
        allowed_symbols=frozenset({"BTC/USD"}),
        crypto_max_qty=Decimal("0.001"),
    )
    broker = FakeBroker()
    client = TestClient(create_app(settings, broker=broker, store=EventStore(settings.db_path)))

    response = client.post(
        "/webhooks/tradingview/pine/preview", content=PINE_ALERT,
        headers={"x-tv-secret": "secret", "content-type": "text/plain"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "lifecycle and protection controls are deferred until Phase 4"
    assert broker.orders == []


def test_pine_paper_submit_is_receipt_time_paper_only_and_persists_broker_status(tmp_path):
    settings = Settings(
        trading_enabled=True,
        webhook_secret="secret",
        db_path=tmp_path / "paper-submit.sqlite3",
        allowed_symbols=frozenset({"BTC/USD"}),
        crypto_max_qty=Decimal("0.001"),
        max_notional=1000.0,
    )
    broker = FakeBroker()
    client = TestClient(create_app(settings, broker=broker, store=EventStore(settings.db_path)))
    alert = PINE_ALERT.replace(
        "QTY=0.001", "QTY=0.0005",
    ).replace(
        "CANCEL_UNFILLED_AT_DEADLINE=YES | PLACE_PROTECTIVE_STOP_AFTER_FILL | "
        "STOP_TRIGGER=65000 | STOP_LIMIT=64950 | ",
        "",
    )

    response = client.post(
        "/webhooks/tradingview/pine/paper-submit", content=alert,
        headers={"x-tv-secret": "secret", "content-type": "text/plain"},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["paper_only"] is True
    assert result["status"] == "accepted"
    assert result["receipt_time_submission"] is True
    assert len(broker.orders) == 1
    # Pine can request a size, but the executable route uses the server-side
    # configured quantity after validating the request against that cap.
    assert broker.orders[0]["qty"] == Decimal("0.001")
    event_id = result["event_id"]
    assert client.app.state.store.status(event_id) == "broker_accepted"


def test_pine_paper_submit_rejects_phase_four_controls_without_broker_call(tmp_path):
    settings = Settings(
        trading_enabled=True,
        webhook_secret="secret",
        db_path=tmp_path / "paper-submit-controls.sqlite3",
        allowed_symbols=frozenset({"BTC/USD"}),
        crypto_max_qty=Decimal("0.001"),
    )
    broker = FakeBroker()
    client = TestClient(create_app(settings, broker=broker, store=EventStore(settings.db_path)))

    response = client.post(
        "/webhooks/tradingview/pine/paper-submit", content=PINE_ALERT,
        headers={"x-tv-secret": "secret", "content-type": "text/plain"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "lifecycle and protection controls are deferred until Phase 4"
    assert broker.orders == []


def test_pine_paper_submit_rejects_unallowlisted_symbol_before_market_lookup(tmp_path):
    settings = Settings(
        trading_enabled=True,
        webhook_secret="secret",
        db_path=tmp_path / "paper-submit-allowlist-order.sqlite3",
        allowed_symbols=frozenset({"QQQ"}),
    )
    broker = FakeBroker()

    def forbidden_market_lookup(_symbol):
        raise AssertionError("unallowlisted Pine symbol reached market data")

    broker.latest_trade_price = forbidden_market_lookup
    client = TestClient(create_app(settings, broker=broker, store=EventStore(settings.db_path)))
    alert = PINE_ALERT.replace("BTCUSD", "AAPL").replace(
        "CANCEL_UNFILLED_AT_DEADLINE=YES | PLACE_PROTECTIVE_STOP_AFTER_FILL | "
        "STOP_TRIGGER=65000 | STOP_LIMIT=64950 | ",
        "",
    )

    response = client.post(
        "/webhooks/tradingview/pine/paper-submit", content=alert,
        headers={"x-tv-secret": "secret", "content-type": "text/plain"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "symbol is not allowlisted"
    assert broker.orders == []


def test_pine_paper_submit_preserves_oversized_body_rejection(tmp_path):
    client, broker = make_app(tmp_path, enabled=True)

    response = client.post(
        "/webhooks/tradingview/pine/paper-submit", content=b"x" * 4097,
        headers={"x-tv-secret": "secret", "content-type": "text/plain"},
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "Pine alert exceeds 4096 byte limit"
    assert broker.orders == []


def test_pine_paper_submit_retains_ambiguous_submission_without_release(tmp_path):
    settings = Settings(
        trading_enabled=True,
        webhook_secret="secret",
        db_path=tmp_path / "paper-submit-ambiguous.sqlite3",
        allowed_symbols=frozenset({"BTC/USD"}),
        crypto_max_qty=Decimal("0.001"),
    )

    broker = FakeBroker()
    def timeout_submit(*_args, **_kwargs):
        raise TimeoutError("receipt lost")
    broker.submit = timeout_submit

    store = EventStore(settings.db_path)
    client = TestClient(create_app(settings, broker=broker, store=store))
    alert = PINE_ALERT.replace(
        "CANCEL_UNFILLED_AT_DEADLINE=YES | PLACE_PROTECTIVE_STOP_AFTER_FILL | "
        "STOP_TRIGGER=65000 | STOP_LIMIT=64950 | ",
        "",
    )
    headers = {"x-tv-secret": "secret", "content-type": "text/plain"}

    first = client.post("/webhooks/tradingview/pine/paper-submit", content=alert, headers=headers)
    second = client.post("/webhooks/tradingview/pine/paper-submit", content=alert, headers=headers)

    assert first.status_code == 503
    assert first.json()["ambiguous"] is True
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert store.status(first.json()["event_id"]) == "submission_ambiguous"


def test_pine_paper_submit_reports_ambiguous_when_receipt_persistence_fails(tmp_path):
    settings = Settings(
        trading_enabled=True,
        webhook_secret="secret",
        db_path=tmp_path / "paper-submit-persistence.sqlite3",
        allowed_symbols=frozenset({"BTC/USD"}),
        crypto_max_qty=Decimal("0.001"),
    )
    broker = FakeBroker()
    store = EventStore(settings.db_path)
    original_update = store.update
    failed_once = False

    def fail_receipt_once(event_id, status, detail="", broker_order_id=None):
        nonlocal failed_once
        if status.startswith("broker_") and not failed_once:
            failed_once = True
            raise OSError("database temporarily unavailable")
        return original_update(event_id, status, detail, broker_order_id)

    store.update = fail_receipt_once
    client = TestClient(create_app(settings, broker=broker, store=store))
    alert = PINE_ALERT.replace(
        "CANCEL_UNFILLED_AT_DEADLINE=YES | PLACE_PROTECTIVE_STOP_AFTER_FILL | "
        "STOP_TRIGGER=65000 | STOP_LIMIT=64950 | ",
        "",
    )

    response = client.post(
        "/webhooks/tradingview/pine/paper-submit", content=alert,
        headers={"x-tv-secret": "secret", "content-type": "text/plain"},
    )

    assert response.status_code == 503
    assert response.json()["ambiguous"] is True
    assert len(broker.orders) == 1
    assert store.status(response.json()["event_id"]) == "submission_ambiguous"


def test_pine_paper_submit_does_not_accept_broker_result_without_order_id(tmp_path):
    settings = Settings(
        trading_enabled=True,
        webhook_secret="secret",
        db_path=tmp_path / "paper-submit-no-order-id.sqlite3",
        allowed_symbols=frozenset({"BTC/USD"}),
        crypto_max_qty=Decimal("0.001"),
    )
    broker = FakeBroker()
    broker.submit = lambda *_args, **_kwargs: BrokerResult("", "accepted", {"status": "accepted"})
    store = EventStore(settings.db_path)
    client = TestClient(create_app(settings, broker=broker, store=store))
    alert = PINE_ALERT.replace(
        "CANCEL_UNFILLED_AT_DEADLINE=YES | PLACE_PROTECTIVE_STOP_AFTER_FILL | "
        "STOP_TRIGGER=65000 | STOP_LIMIT=64950 | ",
        "",
    )

    response = client.post(
        "/webhooks/tradingview/pine/paper-submit", content=alert,
        headers={"x-tv-secret": "secret", "content-type": "text/plain"},
    )

    assert response.status_code == 503
    assert response.json()["ambiguous"] is True
    assert store.status(response.json()["event_id"]) == "submission_ambiguous"
