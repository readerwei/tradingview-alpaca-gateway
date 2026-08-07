"""Acceptance tests for the review findings on a476f53.

Every test here fails on master and should pass once the corresponding fix
lands. They are written against BEHAVIOUR, not implementation, so they do not
dictate how the fix is written — only what must become true.

Covers both the findings I raised and the two TradingBot raised, so the patch
is checked by tests that were not written by whoever wrote it.

One test is marked xfail(strict=True) because it encodes a proposed interface
for a design decision Wei has not made yet. Strict means it flips to a FAILURE
once the collar exists — that is the reminder to delete the marker.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from tv_alpaca_gateway.app import create_app
from tv_alpaca_gateway.broker import FakeBroker
from tv_alpaca_gateway.config import Settings
from tv_alpaca_gateway.models import Signal
from tv_alpaca_gateway.risk import RiskError, approve
from tv_alpaca_gateway.store import EventStore

SECRET = "test-secret"
PATH = "/webhooks/tradingview"


def _settings(tmp_path, **kw):
    base = dict(
        paper_trading=True, trading_enabled=True, webhook_secret=SECRET,
        allowed_symbols=frozenset({"QQQ"}), max_qty=1, max_notional=100_000.0,
        max_alert_age_seconds=180, db_path=tmp_path / "events.sqlite3",
    )
    base.update(kw)
    return Settings(**base)


def _alert(event_id="e1", close=700.0, action="buy", **kw):
    payload = {
        "event_id": event_id, "symbol": "QQQ", "action": action,
        "timeframe": "1", "close": close,
        "bar_time": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(kw)
    return payload


def _client(settings, broker=None, store=None, notifier=None):
    app = create_app(settings, broker, store, notifier)
    return TestClient(app, raise_server_exceptions=False)


# ══════════════════════════════════════════════════ P0 · paper-only guarantee

@pytest.mark.parametrize("url, why", [
    ("https://paper-api.alpaca.markets@api.alpaca.markets",
     "text before '@' is userinfo, not a host — this reaches the LIVE API"),
    ("https://paper-api.alpaca.markets.evil.example.com",
     "attacker-controlled suffix receives APCA-API-SECRET-KEY"),
    ("https://api.alpaca.markets/?x=paper-api.alpaca.markets",
     "the required string appears in the query, not the host"),
])
def test_only_the_real_paper_host_is_accepted(url, why):
    """`"paper-api.alpaca.markets" not in url` is a SUBSTRING test.

    The entire safety claim of this project is that it cannot reach the live
    API. A substring match does not establish that: each URL below contains the
    literal string, so each passes validate(), and none of them resolves to the
    paper host.

    The fix is to compare the parsed hostname exactly, e.g.
        urlsplit(url).hostname == "paper-api.alpaca.markets"
    """
    assert urlsplit(url).hostname != "paper-api.alpaca.markets", "bad fixture"
    with pytest.raises(ValueError):
        Settings(alpaca_base_url=url).validate()


def test_the_genuine_paper_url_still_works():
    """The fix must not be so strict that the real thing stops working."""
    Settings(alpaca_base_url="https://paper-api.alpaca.markets").validate()
    Settings(alpaca_base_url="https://paper-api.alpaca.markets/").validate()


def test_the_live_api_is_refused():
    with pytest.raises(ValueError):
        Settings(alpaca_base_url="https://api.alpaca.markets").validate()


# ═══════════════════════════════════════════ P0 · order state must be truthful

class _BrokenNotifier:
    def send(self, content):
        raise RuntimeError("discord is down")


def test_a_live_order_is_never_recorded_as_failed(tmp_path):
    """notifier.send() runs inside the try that marks the event failed.

    By the time the receipt is sent, broker.submit() has returned and the order
    is working. If Discord is merely unreachable, the except block overwrites
    'submitted' with 'failed' — so the audit trail denies the existence of a
    live order.

    A notification is a courtesy. It must never rewrite order state.
    """
    store = EventStore(tmp_path / "events.sqlite3")
    _client(_settings(tmp_path), FakeBroker(), store, _BrokenNotifier()).post(
        PATH, json=_alert(), headers={"x-tv-secret": SECRET})

    assert store.status("e1") != "failed", (
        "order is live at the broker but the store says it failed")


def test_a_receipt_failure_does_not_return_502(tmp_path):
    """502 tells TradingView the order did not happen, inviting a retry that
    would double the position. The order succeeded; the response must say so."""
    broker = FakeBroker()
    r = _client(_settings(tmp_path), broker, EventStore(tmp_path / "e.sqlite3"),
                _BrokenNotifier()).post(
        PATH, json=_alert(), headers={"x-tv-secret": SECRET})

    assert len(broker.orders) == 1, "fixture: the order should have been placed"
    assert r.status_code == 200, f"got {r.status_code} for a placed order"


# ═════════════════════════════════════ P0 · retry semantics (TradingBot's find)

class _FailingBroker:
    """Fails once, then succeeds — a transient broker timeout."""

    def __init__(self):
        self.calls = 0
        self.orders = []

    def submit(self, order, client_order_id):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("connection timed out")
        from tv_alpaca_gateway.broker import BrokerResult
        self.orders.append(client_order_id)
        return BrokerResult(order_id="ok-1", status="accepted", raw={})


def test_a_transient_broker_failure_can_be_retried(tmp_path):
    """store.claim() consumes the event_id before the order is submitted.

    When submission fails the event is marked 'failed' but the id stays
    claimed, so TradingView's retry of the SAME alert is answered
    'duplicate' and the trade is silently lost — the one case where a retry
    is both safe and necessary.

    'Already claimed' must not be conflated with 'already submitted'.
    """
    broker = _FailingBroker()
    store = EventStore(tmp_path / "events.sqlite3")
    client = _client(_settings(tmp_path), broker, store)

    first = client.post(PATH, json=_alert(), headers={"x-tv-secret": SECRET})
    assert first.status_code == 502

    second = client.post(PATH, json=_alert(), headers={"x-tv-secret": SECRET})
    assert second.json().get("duplicate") is not True, "retry refused as duplicate"
    assert broker.calls == 2, "the retry never reached the broker"


def test_a_successful_order_is_still_not_repeatable(tmp_path):
    """The other half of the contract: making retries possible must not make
    genuine duplicates possible. Same event_id, already submitted → refused."""
    store = EventStore(tmp_path / "events.sqlite3")
    broker = FakeBroker()
    client = _client(_settings(tmp_path), broker, store)

    client.post(PATH, json=_alert(), headers={"x-tv-secret": SECRET})
    client.post(PATH, json=_alert(), headers={"x-tv-secret": SECRET})

    assert len(broker.orders) == 1, "the same alert placed two orders"


# ══════════════════════════════════════════════════════ P1 · risk gates bind

def test_max_qty_is_honoured_not_hardcoded_to_one(tmp_path):
    """risk.py: `qty = min(settings.max_qty, 1)` — always 1.

    MAX_QTY=5 silently yields 1 share. Either honour the setting, or delete
    both the setting and its validate() check and invert this test — what is
    not acceptable is config that reads as a limit and does nothing.
    """
    order = approve(Signal.parse(_alert(close=100.0)),
                    _settings(tmp_path, max_qty=5, max_notional=100_000.0))
    assert order.qty == 5


def test_notional_cap_actually_binds(tmp_path):
    """With qty pinned to 1, `qty * close > max_notional` degrades to
    `close > max_notional` — it can only ever reject a single share priced
    above the cap. A $1,000 cap must reject 5 x $700."""
    with pytest.raises(RiskError):
        approve(Signal.parse(_alert(close=700.0)),
                _settings(tmp_path, max_qty=5, max_notional=1_000.0))


# ═════════════════════════════════ P1 · client_order_id collision (new finding)

def test_distinct_events_get_distinct_client_order_ids(tmp_path):
    """`client_order_id = f"tv-{event_id[:110]}"` truncates at 110 chars.

    event_id is validated up to 256, so two distinct alerts sharing a long
    prefix collapse to one client_order_id. The store treats them as separate
    events and submits both; Alpaca rejects the second as a duplicate
    client_order_id, so a legitimate second trade fails for a reason that
    appears nowhere in the logs.

    Hash the event_id instead of truncating it.
    """
    broker = FakeBroker()
    store = EventStore(tmp_path / "events.sqlite3")
    client = _client(_settings(tmp_path), broker, store)

    for suffix in ("1", "2"):
        client.post(PATH, json=_alert(event_id="A" * 200 + suffix),
                    headers={"x-tv-secret": SECRET})

    ids = [o["client_order_id"] for o in broker.orders]
    assert len(ids) == 2, "fixture: both alerts should have been submitted"
    assert len(set(ids)) == 2, f"collision: both events produced {ids[0]!r}"


# ═══════════════════════════════════════ P1 · price collar (design undecided)

@pytest.mark.xfail(strict=True, reason=(
    "No collar exists yet and the design is Wei's call: hard reject vs "
    "accept-and-alert. This encodes the minimal contract — an order priced "
    "absurdly far from a market reference must not be placed. Adjust to match "
    "whatever interface is chosen, then remove this marker."))
def test_limit_price_is_collared_against_a_market_reference(tmp_path):
    """limit_price is the payload's `close`, unchecked against anything.

    A corrupted alert (close=7.0 on a $700 instrument) becomes a resting order
    100x away from market. The notional gate cannot catch it because it uses
    that same claimed number — understating the price makes the gate EASIER to
    pass while real exposure is unchanged.
    """
    with pytest.raises(RiskError):
        approve(Signal.parse(_alert(close=7.0, action="sell")),
                _settings(tmp_path), reference_price=700.0)


# ═════════════════════════════════════════════════════ regression guards

def test_the_secret_still_gates_the_endpoint(tmp_path):
    """None of the fixes may weaken authentication."""
    client = _client(_settings(tmp_path), FakeBroker())
    assert client.post(PATH, json=_alert()).status_code == 401
    assert client.post(PATH, json=_alert(),
                       headers={"x-tv-secret": "wrong"}).status_code == 401


def test_the_kill_switch_still_stops_everything(tmp_path):
    broker = FakeBroker()
    r = _client(_settings(tmp_path, trading_enabled=False), broker).post(
        PATH, json=_alert(), headers={"x-tv-secret": SECRET})
    assert r.json()["executed"] is False
    assert broker.orders == [], "kill switch did not stop the order"


def test_a_stale_alert_is_still_refused(tmp_path):
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with pytest.raises(RiskError):
        approve(Signal.parse(_alert(bar_time=old)), _settings(tmp_path))


def test_an_unlisted_symbol_is_still_refused(tmp_path):
    with pytest.raises(RiskError):
        approve(Signal.parse(_alert(symbol="TSLA")), _settings(tmp_path))
