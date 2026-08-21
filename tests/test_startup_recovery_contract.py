"""An entry that filled and was never protected must be findable at startup.

PR #71 tells the operator, in a CRITICAL log line, that "reconcile on next
start is the net". That was false when it was written. Three mechanisms each
decline to help:

  * `_open_managed_lot` writes the lot row AFTER protection succeeds, so a
    crash in between leaves no lot for `supervisor.start()` to re-arm;
  * `broker_filled` is in `EventStore.TERMINAL`, so `unresolved_broker_orders`
    — the reconnect resync — deliberately skips the one order that needs help;
  * re-firing the same alert by hand is refused as a duplicate, so the
    operator's obvious remedy silently does nothing.

The window is real and acknowledged: the route answers 202 and protection runs
in a background task that can poll for up to `deadline_seconds` before placing
anything. A restart in that window leaves a filled, unprotected position that
nothing looks for.

Found by an independent review agent. The comment promising the net is mine,
which makes it worse than the gap: it would stop the next person building one.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from tv_alpaca_gateway.store import EventStore


def _store(tmp_path) -> EventStore:
    return EventStore(tmp_path / "recovery.sqlite3")


def _filled_entry(store, event_id="pine-exec-evt-1", order_id="ord-1"):
    """An entry that filled. Exactly the state a crash-before-protection leaves."""
    store.claim(event_id)
    store.update(event_id, "broker_filled", "filled", broker_order_id=order_id)
    store.record_broker_order(order_id, event_id, "entry", "filled")


# ── the query ───────────────────────────────────────────────────────────────

def test_a_filled_entry_with_no_protection_is_findable(tmp_path):
    store = _store(tmp_path)
    _filled_entry(store)

    assert store.filled_without_protection() == ["pine-exec-evt-1"]


def test_a_protected_entry_is_not_reported(tmp_path):
    store = _store(tmp_path)
    _filled_entry(store)
    store.record_broker_order("ord-2", "pine-exec-evt-1", "protection", "new")

    assert store.filled_without_protection() == []


def test_a_flattened_entry_is_not_reported(tmp_path):
    """Flattening IS a resolution — the position is gone, nothing to recover."""
    store = _store(tmp_path)
    _filled_entry(store)
    store.record_broker_order("ord-3", "pine-exec-evt-1", "flatten", "filled")

    assert store.filled_without_protection() == []


def test_an_entry_that_never_filled_is_not_reported(tmp_path):
    store = _store(tmp_path)
    store.claim("pine-exec-evt-2")
    store.update("pine-exec-evt-2", "broker_canceled", "", broker_order_id="ord-9")
    store.record_broker_order("ord-9", "pine-exec-evt-2", "entry", "canceled")

    assert store.filled_without_protection() == []


def test_a_lot_that_opened_counts_as_protected(tmp_path):
    """A managed lot places its own disaster stop, recorded as `protection`."""
    store = _store(tmp_path)
    _filled_entry(store)
    store.record_broker_order("ord-4", "pine-exec-evt-1", "protection", "new")
    store.save_lot("pine-exec-evt-1", "QQQ", "ladder", "{}")

    assert store.filled_without_protection() == []


# ── the sweep, wired into startup ───────────────────────────────────────────

def test_startup_reports_an_unprotected_fill_at_critical(tmp_path, caplog):
    """The net that #71 promised. Detection, loudly, before anything trades."""
    from fastapi.testclient import TestClient

    from tv_alpaca_gateway.app import create_app
    from tv_alpaca_gateway.broker import FakeBroker
    from tv_alpaca_gateway.config import Settings

    settings = Settings(
        paper_trading=True, trading_enabled=True, webhook_secret="s",
        allowed_symbols=frozenset({"QQQ"}), max_qty=10, max_notional=10_000.0,
        db_path=tmp_path / "startup.sqlite3")
    store = EventStore(settings.db_path)
    _filled_entry(store)

    with caplog.at_level(logging.CRITICAL, logger="tv_alpaca_gateway"):
        with TestClient(create_app(settings, FakeBroker(), store)) as client:
            client.get("/healthz")

    assert "pine-exec-evt-1" in caplog.text, (
        "startup did not report an entry that filled and was never protected")
    assert any(r.levelno >= logging.CRITICAL for r in caplog.records), (
        "an unprotected fill was reported below CRITICAL")


def test_healthz_surfaces_unprotected_fills(tmp_path):
    """A log line at boot scrolls away. The count has to be answerable later."""
    from fastapi.testclient import TestClient

    from tv_alpaca_gateway.app import create_app
    from tv_alpaca_gateway.broker import FakeBroker
    from tv_alpaca_gateway.config import Settings

    settings = Settings(
        paper_trading=True, trading_enabled=True, webhook_secret="s",
        allowed_symbols=frozenset({"QQQ"}), max_qty=10, max_notional=10_000.0,
        db_path=tmp_path / "startup2.sqlite3")
    store = EventStore(settings.db_path)
    _filled_entry(store)

    with TestClient(create_app(settings, FakeBroker(), store)) as client:
        body = client.get("/healthz").json()

    assert body.get("unprotected_fills") == ["pine-exec-evt-1"], (
        f"/healthz does not surface unprotected fills: {body.get('unprotected_fills')}")


def test_a_clean_store_reports_nothing(tmp_path):
    from fastapi.testclient import TestClient

    from tv_alpaca_gateway.app import create_app
    from tv_alpaca_gateway.broker import FakeBroker
    from tv_alpaca_gateway.config import Settings

    settings = Settings(
        paper_trading=True, trading_enabled=True, webhook_secret="s",
        allowed_symbols=frozenset({"QQQ"}), max_qty=10, max_notional=10_000.0,
        db_path=tmp_path / "clean.sqlite3")
    with TestClient(create_app(settings, FakeBroker(), EventStore(settings.db_path))) as client:
        assert client.get("/healthz").json().get("unprotected_fills") == []
