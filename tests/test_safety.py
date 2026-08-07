from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from tv_alpaca_gateway.app import create_app
from tv_alpaca_gateway.broker import FakeBroker
from tv_alpaca_gateway.config import Settings
from tv_alpaca_gateway.store import EventStore


def payload(event_id="safety-test", close=700):
    return {
        "event_id": event_id,
        "symbol": "QQQ",
        "action": "buy",
        "timeframe": "1m",
        "bar_time": datetime.now(timezone.utc).isoformat(),
        "close": close,
    }


def test_paper_url_requires_exact_hostname():
    for url in (
        "https://paper-api.alpaca.markets.evil.example.com",
        "https://paper-api.alpaca.markets@api.alpaca.markets",
        "https://api.alpaca.markets",
    ):
        with pytest.raises(ValueError, match="paper API URL"):
            Settings(alpaca_base_url=url).validate()


def test_max_qty_is_enforced_and_price_collar_rejects_bad_alert(tmp_path):
    settings = Settings(
        trading_enabled=True,
        webhook_secret="secret",
        db_path=tmp_path / "events.sqlite3",
        max_qty=3,
        max_notional=3_000,
    )
    broker = FakeBroker()
    client = TestClient(create_app(settings, broker=broker, store=EventStore(settings.db_path)))
    headers = {"x-tv-secret": "secret"}

    accepted = client.post("/webhooks/tradingview", json=payload("qty", 700), headers=headers)
    assert accepted.status_code == 200
    assert broker.orders[0]["qty"] == 3

    rejected = client.post("/webhooks/tradingview", json=payload("collar", 7), headers=headers)
    assert rejected.status_code == 403
    assert "deviates" in rejected.json()["detail"]
    assert len(broker.orders) == 1


class FailingNotifier:
    def send(self, message):
        raise RuntimeError("discord is down")


def test_receipt_failure_does_not_rewrite_submitted_order(tmp_path):
    settings = Settings(
        trading_enabled=True,
        webhook_secret="secret",
        db_path=tmp_path / "events.sqlite3",
    )
    store = EventStore(settings.db_path)
    client = TestClient(
        create_app(settings, broker=FakeBroker(), store=store, notifier=FailingNotifier())
    )
    response = client.post(
        "/webhooks/tradingview",
        json=payload("notification-failure"),
        headers={"x-tv-secret": "secret"},
    )
    assert response.status_code == 200
    assert store.status("notification-failure") == "submitted"
