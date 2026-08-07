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
