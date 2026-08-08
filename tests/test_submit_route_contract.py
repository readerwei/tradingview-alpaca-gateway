"""Contract for the route that turns a relayed Pine alert into a real order.

This is the last gap between "the relay works" and "I can send orders from
Discord", and it is the one worth slowing down for: everything before it was
structurally unable to trade, and this is the change that removes that.

TWO SAFETY BOUNDARIES HAVE TO MOVE, DELIBERATELY
------------------------------------------------
1. The relay currently refuses any target but ``/pine/dry-run``. That check is
   what makes the relay safe today, so widening it must be an explicit, opt-in
   act with its own setting — not a URL edit.

2. A route that submits must delegate to ``execute_pine_command``. The earlier
   draft did its own parse -> risk -> claim -> submit, which meant two
   independent execution paths where only one was covered by the Stage 3
   contract. The un-contracted one was the one wired to a route.

WHAT THIS FILE DOES NOT RE-SPECIFY
----------------------------------
Order lifecycle — sizing from the position, protective stops, retry-then-
flatten, the deadline — all belong to tests/test_execution_contract.py and are
asserted there. If the route delegates properly, it inherits every one of them.
Duplicating them here would create a second definition that can drift, which is
the exact failure this contract exists to prevent.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tv_alpaca_gateway.config import Settings
from tv_alpaca_gateway.store import EventStore

# Carries EVENT_ID and BAR_TIME because tests/test_alert_identity_contract.py
# requires them: an alert that cannot be identified must be refused, and one
# that cannot be dated cannot be checked for staleness.
#
# This fixture and that contract have to agree. They did not at first — this
# file was written before the identity flaw was found, and its alert had
# neither field, so every case here would have failed the moment identity
# landed. That is the same "second definition free to drift" this file's own
# docstring warns about, committed in the fixture instead of the assertions.
#
# BAR_TIME is generated fresh per run; a hardcoded timestamp would go stale and
# start failing the freshness rule some hours after being written.
def _fresh_alert(event_id="QQQ-1-route-test"):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ("EXECUTE_ALPACA_ORDER | SYMBOL=QQQ | SIDE=BUY | QTY=1 | "
            "ORDER_TYPE=MARKET | TIME_IN_FORCE=DAY | "
            f"EVENT_ID={event_id} | BAR_TIME={now}")


ALERT = _fresh_alert()
SECRET = "s3cret"
SUBMIT_PATH = "/webhooks/tradingview/pine/submit"


def _settings(tmp_path, **kw):
    base = dict(
        paper_trading=True, trading_enabled=True, webhook_secret=SECRET,
        allowed_symbols=frozenset({"QQQ", "BTC/USD"}),
        max_qty=3, crypto_max_qty=Decimal("0.05"), max_notional=3500.0,
        db_path=tmp_path / "submit.sqlite3",
    )
    base.update(kw)
    return Settings(**base)


def _client(settings, broker=None, store=None, notifier=None):
    from fastapi.testclient import TestClient

    from tv_alpaca_gateway.app import create_app
    return TestClient(create_app(settings, broker, store or EventStore(settings.db_path),
                                 notifier), raise_server_exceptions=False)


def _post(client, body=None, secret=SECRET):
    return client.post(SUBMIT_PATH, content=body if body is not None else _fresh_alert(),
                       headers={"x-tv-secret": secret})


class _RecordingBroker:
    def __init__(self):
        self.submitted = []

    def latest_trade_price(self, symbol):
        return 722.70

    def submit_order(self, **kwargs):
        self.submitted.append(dict(kwargs))
        return {"id": f"ord-{len(self.submitted)}", "status": "filled",
                "filled_qty": str(kwargs["qty"])}

    def position_qty(self, symbol):
        return Decimal(str(sum(Decimal(str(o["qty"])) for o in self.submitted
                               if o["side"] == "buy")))

    def cancel_order(self, order_id):
        pass


# ═══════════════════════════════════════════ the route must not be a second path

def test_the_route_delegates_to_the_contracted_engine(tmp_path, monkeypatch):
    """A route with its own parse -> risk -> submit is a second execution path.

    The first draft had exactly that, and only the engine was covered by the
    Stage 3 contract — so the lifecycle rules were enforced on the path nobody
    was using and absent from the one wired to a route.
    """
    execution = pytest.importorskip("tv_alpaca_gateway.execution")
    calls = []
    monkeypatch.setattr(execution, "execute_pine_command",
                        lambda *a, **k: calls.append((a, k)) or execution.ExecutionResult("ord-1"))

    broker = _RecordingBroker()
    _post(_client(_settings(tmp_path), broker))

    assert calls, ("the route submitted without calling execute_pine_command; "
                   "the Stage 3 lifecycle rules do not apply to it")


# ══════════════════════════════════════════════════════ the gates still hold

def test_the_submit_route_requires_the_secret(tmp_path):
    client = _client(_settings(tmp_path), _RecordingBroker())
    assert client.post(SUBMIT_PATH, content=ALERT).status_code == 401
    assert _post(client, secret="wrong").status_code == 401


def test_the_kill_switch_stops_the_route(tmp_path):
    """TRADING_ENABLED=false must stop an order at the route, not merely
    somewhere downstream."""
    broker = _RecordingBroker()
    response = _post(_client(_settings(tmp_path, trading_enabled=False), broker))

    assert broker.submitted == [], "the kill switch did not stop the order"
    assert response.status_code < 500


def test_an_oversized_body_is_refused(tmp_path):
    broker = _RecordingBroker()
    response = _post(_client(_settings(tmp_path), broker), body="X" * 5000)

    assert response.status_code == 413
    assert broker.submitted == []


def test_a_malformed_alert_never_reaches_the_broker(tmp_path):
    broker = _RecordingBroker()
    response = _post(_client(_settings(tmp_path), broker), body="hello")

    assert response.status_code == 422
    assert broker.submitted == []


# ═══════════════════════════════════════════════ idempotency across the wire

def test_the_same_alert_posted_twice_places_one_order(tmp_path):
    """Discord redelivery, a relay restart, a doubled webhook — none may double
    the position. This is the failure with the largest cost and the smallest
    warning."""
    settings = _settings(tmp_path)
    store = EventStore(settings.db_path)
    broker = _RecordingBroker()
    client = _client(settings, broker, store)

    # The SAME alert twice — same EVENT_ID, so one order. A different EVENT_ID
    # would be a different firing and must be allowed to trade.
    repeated = _fresh_alert(event_id="QQQ-1-repeated")
    _post(client, repeated)
    _post(client, repeated)

    entries = [o for o in broker.submitted if o.get("type", "market") == "market"]
    assert len(entries) == 1, "the same alert entered twice"


def test_the_response_carries_the_broker_order_id(tmp_path):
    """Without it there is nothing to reconcile against, and no way to answer
    "did my alert actually do anything" except by reading the database."""
    broker = _RecordingBroker()
    body = _post(_client(_settings(tmp_path), broker)).json()

    assert body.get("order_id") or body.get("entry_order_id"), (
        f"no broker order id in the response: {body}")


# ═════════════════════════ widening the relay must be deliberate and opt-in

def test_the_relay_still_refuses_an_executing_target_by_default(tmp_path):
    """The relay's dry-run restriction is what makes it safe today.

    Reaching a submit route must require an explicit setting, so that pointing
    the relay at execution is a decision someone made rather than a URL they
    edited.
    """
    from tv_alpaca_gateway.relay import RelaySettings

    settings = RelaySettings(
        token="t", channel_id=1, source_webhook_id=2,
        internal_url=f"http://127.0.0.1:8000{SUBMIT_PATH}", internal_secret="s")

    with pytest.raises(ValueError):
        settings.validate_target()


def test_the_relay_can_be_opted_into_execution_explicitly(tmp_path):
    """And once opted in, it must still refuse everything else — a flag that
    widens the target to 'anything' is not a gate."""
    from tv_alpaca_gateway.relay import RelaySettings

    opted_in = dict(token="t", channel_id=1, source_webhook_id=2,
                    internal_secret="s", allow_execution=True)

    RelaySettings(internal_url=f"http://127.0.0.1:8000{SUBMIT_PATH}",
                  **opted_in).validate_target()

    for bad in ("http://127.0.0.1:8000/webhooks/tradingview",
                "http://evil.example.com:8000" + SUBMIT_PATH):
        with pytest.raises(ValueError):
            RelaySettings(internal_url=bad, **opted_in).validate_target()
