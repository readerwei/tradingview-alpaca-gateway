"""Tests that actually run the handshake, the reconnect loop and shutdown.

The existing stream tests monkeypatch `run_forever` away and feed dicts to the
parsers, so `connect`, `authenticate`, `_run_once`, `_await_frame`,
`_run_with_reconnect` and `stop` executed in NO test. Three bugs that make the
feature completely non-functional lived in exactly those lines, and the suite
was green throughout.

The fake servers here replay Alpaca's documented frames — including the
greeting that broke the original handshake — so they fail against a wrong
protocol instead of agreeing with it.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from tv_alpaca_gateway.config import Settings
from tv_alpaca_gateway.stream import (
    AlpacaMarketStream,
    AlpacaStreamManager,
    AlpacaTradeUpdateStream,
    _MarketDataSocket,
    _run_with_reconnect,
    _TradingSocket,
)

SETTINGS = Settings(alpaca_key_id="key", alpaca_secret_key="secret",
                    stream_enabled=True)


class FakeSocket:
    """Replays scripted frames and records what was sent."""

    def __init__(self, frames: list):
        self._frames = [json.dumps(f) for f in frames]
        self.sent: list[dict] = []
        self.reads = 0
        self.reads_before_first_send: int | None = None

    async def send(self, payload: str) -> None:
        if self.reads_before_first_send is None:
            self.reads_before_first_send = self.reads
        self.sent.append(json.loads(payload))

    async def recv(self) -> str:
        self.reads += 1
        if not self._frames:
            # A real socket blocks here; the handshake must not depend on a
            # frame the server never sends.
            raise asyncio.TimeoutError("no more frames")
        return self._frames.pop(0)


# ───────────────────────────────────────────────── market-data handshake

def test_market_handshake_waits_for_the_greeting_before_sending_auth():
    """Alpaca greets with {"T":"success","msg":"connected"} BEFORE auth.

    Two bugs here, and the second is nastier than the first. Reading exactly
    one frame after sending auth reads the greeting rather than the auth reply.
    Skipping the greeting afterwards fixes that — but auth sent BEFORE the
    greeting arrives is sometimes ignored, leaving the socket open and silent
    until the handshake times out and reconnects, forever.

    Observed live: three runs connected and streamed ticks, the fourth hung.
    Anything that only works when the greeting happens to land first is a race.
    """
    sock = FakeSocket([
        [{"T": "success", "msg": "connected"}],     # the greeting
        [{"T": "success", "msg": "authenticated"}],
    ])
    asyncio.run(_MarketDataSocket(SETTINGS, SETTINGS.market_stream_url)
                .authenticate(sock))

    assert sock.sent[0] == {"action": "auth", "key": "key", "secret": "secret"}
    assert sock.reads_before_first_send == 1, (
        "auth was sent before waiting for the greeting")


def test_market_handshake_survives_a_missing_greeting():
    """The greeting is waited for, not required — an endpoint that goes
    straight to the auth reply must still connect."""
    sock = FakeSocket([[{"T": "success", "msg": "authenticated"}]])
    asyncio.run(_MarketDataSocket(SETTINGS, SETTINGS.market_stream_url)
                .authenticate(sock))
    assert sock.sent[0]["action"] == "auth"


def test_market_subscribe_uses_the_market_protocol():
    """Market data subscribes with `subscribe`; `listen` belongs to the trading
    stream and is meaningless here."""
    sock = FakeSocket([[{"T": "subscription", "trades": ["QQQ"], "quotes": ["QQQ"]}]])
    asyncio.run(_MarketDataSocket(SETTINGS, SETTINGS.market_stream_url)
                .subscribe(sock, ["QQQ"]))

    assert sock.sent[0]["action"] == "subscribe"
    assert sock.sent[0]["quotes"] == ["QQQ"]


def test_market_auth_failure_is_raised_not_swallowed():
    sock = FakeSocket([[{"T": "error", "code": 402, "msg": "auth failed"}]])
    with pytest.raises(RuntimeError, match="auth failed"):
        asyncio.run(_MarketDataSocket(SETTINGS, SETTINGS.market_stream_url)
                    .authenticate(sock))


# ──────────────────────────────────────────────────── trading handshake

def test_trading_handshake_uses_the_trading_protocol():
    """The trading stream is a DIFFERENT protocol: `authenticate` with nested
    key_id/secret_key, and replies that carry no `T` field at all."""
    sock = FakeSocket([{"stream": "authorization",
                        "data": {"status": "authorized", "action": "authenticate"}}])
    asyncio.run(_TradingSocket(SETTINGS, SETTINGS.trade_stream_url).authenticate(sock))

    assert sock.sent[0] == {
        "action": "authenticate",
        "data": {"key_id": "key", "secret_key": "secret"},
    }


def test_trading_listen_is_confirmed():
    sock = FakeSocket([{"stream": "listening", "data": {"streams": ["trade_updates"]}}])
    asyncio.run(_TradingSocket(SETTINGS, SETTINGS.trade_stream_url)
                .listen(sock, ["trade_updates"]))
    assert sock.sent[0] == {"action": "listen",
                            "data": {"streams": ["trade_updates"]}}


def test_unauthorized_is_an_error_not_a_silent_retry():
    """A bad key must surface. Previously it looked like any other disconnect
    and retried forever behind a warning log."""
    sock = FakeSocket([{"stream": "authorization", "data": {"status": "unauthorized"}}])
    with pytest.raises(RuntimeError, match="authorization"):
        asyncio.run(_TradingSocket(SETTINGS, SETTINGS.trade_stream_url).authenticate(sock))


def test_a_silent_server_does_not_hang_the_handshake():
    """Bounded by frame count, so a server that never sends the expected reply
    fails cleanly instead of parking the task forever."""
    sock = FakeSocket([[{"T": "success", "msg": "connected"}]] * 20)
    with pytest.raises(RuntimeError, match="within"):
        asyncio.run(_MarketDataSocket(SETTINGS, SETTINGS.market_stream_url)
                    .authenticate(sock))


# ─────────────────────────────────────────────────────────── shutdown

def test_stop_returns_even_when_a_stream_is_healthy():
    """A task parked in `async for raw in websocket` never observes stop_event.

    The original stop() set the event and awaited the tasks, so a HEALTHY
    stream — the normal case — hung application shutdown forever.
    """
    async def scenario():
        manager = AlpacaStreamManager(SETTINGS)

        async def never_returns(stop_event):
            await asyncio.Event().wait()

        manager.market.run_forever = never_returns
        manager.trade_updates.run_forever = never_returns
        await manager.start()
        await asyncio.sleep(0)
        await asyncio.wait_for(manager.stop(timeout=0.2), timeout=5.0)
        assert manager.tasks == []

    asyncio.run(scenario())


def test_stop_lets_a_cooperative_stream_exit_cleanly():
    """Cancellation is the backstop, not the mechanism: a task that does watch
    the event still gets to finish on its own terms."""
    async def scenario():
        manager = AlpacaStreamManager(SETTINGS)
        exited = []

        async def cooperative(stop_event):
            await stop_event.wait()
            exited.append(True)

        manager.market.run_forever = cooperative
        manager.trade_updates.run_forever = cooperative
        await manager.start()
        await asyncio.sleep(0)
        await manager.stop(timeout=2.0)
        assert len(exited) == 2, "tasks were cancelled instead of exiting cleanly"

    asyncio.run(scenario())


# ───────────────────────────────────────────────────── reconnect policy

def test_a_clean_server_close_still_backs_off():
    """`run_once()` returning normally means the server closed the socket.

    That was treated as success and reset the delay to 1s, so a server closing
    on every accept produced a hot reconnect loop rather than backoff.
    """
    async def scenario():
        stop = asyncio.Event()
        waits: list[float] = []
        attempts = 0

        async def closes_immediately():
            nonlocal attempts
            attempts += 1
            if attempts >= 4:
                stop.set()

        real_wait_for = asyncio.wait_for

        async def recording_wait_for(aw, timeout):
            waits.append(timeout)
            return await real_wait_for(aw, 0)      # don't actually sleep

        asyncio.wait_for = recording_wait_for
        try:
            await _run_with_reconnect(closes_immediately, stop, None, "test")
        finally:
            asyncio.wait_for = real_wait_for

        assert waits == sorted(waits), f"backoff did not grow: {waits}"
        assert waits[-1] > waits[0], f"backoff never increased: {waits}"

    asyncio.run(scenario())


# ──────────────────────────────────────────────────── reconnect resync

def test_reconnect_resyncs_orders_before_reading():
    """Alpaca does not replay trade_updates missed while disconnected.

    Without a resync, a fill during the outage is lost permanently and the
    store keeps the pre-outage status — believing a position is working when it
    already filled.
    """
    async def scenario():
        called = []

        async def on_connected():
            called.append("resync")

        stream = AlpacaTradeUpdateStream(SETTINGS, on_connected=on_connected)

        class Sock(FakeSocket):
            def __aiter__(self):
                assert called == ["resync"], "read the socket before resyncing"
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        sock = Sock([
            {"stream": "authorization", "data": {"status": "authorized"}},
            {"stream": "listening", "data": {"streams": ["trade_updates"]}},
        ])

        class _Ctx:
            async def __aenter__(self): return sock
            async def __aexit__(self, *a): return False

        stream.connect = lambda: _Ctx()
        await stream._run_once()
        assert called == ["resync"]

    asyncio.run(scenario())


# ────────────────────────────── connection limit (Alpaca allows one per endpoint)

def test_a_connection_limit_refusal_is_distinct_from_a_disconnect():
    """Alpaca answers a second connection with code 406.

    Retrying cannot clear it — another client holds the only slot — so it must
    not look like ordinary flakiness. It previously surfaced as a warning inside
    the reconnect loop, so a stream that could never succeed appeared to be
    retrying transiently, and `scripts/ticks.py` looked broken when it was being
    refused.
    """
    from tv_alpaca_gateway.stream import ConnectionLimitExceeded

    sock = FakeSocket([[{"T": "error", "code": 406,
                         "msg": "connection limit exceeded"}]])
    with pytest.raises(ConnectionLimitExceeded, match="one connection per account"):
        asyncio.run(_MarketDataSocket(SETTINGS, SETTINGS.market_stream_url)
                    .authenticate(sock))


def test_the_limit_message_names_what_to_stop():
    """A 406 is fixed by stopping the other client, so the message has to say
    that — the raw text "connection limit exceeded" does not."""
    from tv_alpaca_gateway.stream import ConnectionLimitExceeded

    sock = FakeSocket([[{"T": "error", "code": 406, "msg": "connection limit exceeded"}]])
    try:
        asyncio.run(_MarketDataSocket(SETTINGS, SETTINGS.market_stream_url)
                    .authenticate(sock))
        pytest.fail("expected a refusal")
    except ConnectionLimitExceeded as exc:
        assert "ticks.py" in str(exc) and SETTINGS.market_stream_url in str(exc)


def test_other_error_codes_remain_ordinary_failures():
    """Only 406 is special; a bad key must not be mistaken for a busy slot."""
    from tv_alpaca_gateway.stream import ConnectionLimitExceeded

    sock = FakeSocket([[{"T": "error", "code": 402, "msg": "auth failed"}]])
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(_MarketDataSocket(SETTINGS, SETTINGS.market_stream_url)
                    .authenticate(sock))
    assert not isinstance(caught.value, ConnectionLimitExceeded)
