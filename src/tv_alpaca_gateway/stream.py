"""Persistent Alpaca WebSockets: market data and order updates.

TWO PROTOCOLS, NOT ONE
----------------------
Alpaca's market-data stream and its trading (``trade_updates``) stream are
different protocols that happen to both be WebSockets. Sharing one handshake
between them is why neither could connect:

    market data   {"action":"auth","key":K,"secret":S}
                  -> [{"T":"success","msg":"authenticated"}]
                  {"action":"subscribe","quotes":[...],"trades":[...]}
                  -> [{"T":"subscription",...}]

    trading       {"action":"authenticate","data":{"key_id":K,"secret_key":S}}
                  -> {"stream":"authorization","data":{"status":"authorized"}}
                  {"action":"listen","data":{"streams":["trade_updates"]}}
                  -> {"stream":"listening","data":{"streams":[...]}}

Note the trading replies carry no ``T`` field at all, so a check for
``T == "success"`` rejects even a successful authorization.

VERIFIED AGAINST LIVE ALPACA (paper keys, read-only, no orders placed)
----------------------------------------------------------------------
Running the previous implementation against the real endpoints produced exactly
these frames, so none of this is theoretical:

    market   unexpected response during authenticated:
             [{'T': 'success', 'msg': 'connected'}]
    trading  unexpected response during authenticated:
             [{'stream': 'authorization',
               'data': {'action': 'authenticate', 'status': 'authorized'}}]

The trading line rewards a second read: the status is **authorized**. Alpaca
accepted the old payload, and the client then rejected its own success because
it was hunting for a ``T`` field this protocol never sends. That bug was in
reading the reply, not in sending the request. The request is sent in the
documented form here regardless, and that is verified working too.

Reproduce with ``scripts/smoke_stream.py``.

THE GREETING FRAME
------------------
Alpaca sends ``[{"T":"success","msg":"connected"}]`` the moment the market-data
socket opens, before any authentication. Reading exactly one frame after
sending auth therefore reads the greeting, not the auth reply. Handshakes here
scan forward for the frame they want — bounded, so a chatty or hostile server
cannot hold the connection open forever.

WHY THE FAILURE WAS INVISIBLE
-----------------------------
Every one of those bugs sat behind ``_run_with_reconnect``, which logs a
warning and retries. Both streams would fail authentication forever while
``/healthz`` reported ``ok`` and no order updates arrived. Getting this wrong
does not look broken; it looks quiet.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import websockets
from websockets.exceptions import ConnectionClosed

from .config import Settings

logger = logging.getLogger(__name__)

Callback = Callable[[Any], Any | Awaitable[Any]]

# A handshake reply should arrive within a few frames. The cap stops a server
# that streams unrelated messages from stalling the handshake indefinitely.
_HANDSHAKE_FRAME_LIMIT = 10
_HANDSHAKE_TIMEOUT_S = 15.0
# The greeting is waited for, not required: an endpoint that goes straight to
# the auth reply must not pay the full handshake timeout before we send auth.
_GREETING_TIMEOUT_S = 5.0

# Reconnect backoff is only reset once a connection has proved itself. A socket
# that dies immediately must not reset the delay, or a server closing on every
# accept produces a hot reconnect loop instead of backing off.
_STABLE_CONNECTION_S = 30.0


@dataclass(frozen=True)
class MarketQuote:
    symbol: str
    timestamp: str
    bid_price: float
    bid_size: float
    ask_price: float
    ask_size: float
    raw: dict[str, Any]


@dataclass(frozen=True)
class MarketTrade:
    symbol: str
    timestamp: str
    price: float
    size: float
    trade_id: int | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class OrderUpdate:
    event: str
    order_id: str
    client_order_id: str
    symbol: str
    side: str
    status: str
    qty: str
    filled_qty: str
    raw: dict[str, Any]

    @property
    def is_partial_fill(self) -> bool:
        return self.event == "partial_fill" or self.status == "partially_filled"

    @property
    def is_fill(self) -> bool:
        return self.event in {"fill", "partial_fill"} or self.status in {
            "filled",
            "partially_filled",
        }

    @property
    def is_terminal(self) -> bool:
        return self.status in {"canceled", "rejected", "expired", "done_for_day"}


async def _invoke(callback: Callback | None, value: Any) -> None:
    if callback is None:
        return
    result = callback(value)
    if inspect.isawaitable(result):
        await result


def parse_market_message(message: dict[str, Any]) -> MarketQuote | MarketTrade | None:
    """Convert one Alpaca market-data message into a typed event."""
    message_type = message.get("T")
    if message_type == "q":
        return MarketQuote(
            symbol=str(message["S"]),
            timestamp=str(message["t"]),
            bid_price=float(message["bp"]),
            bid_size=float(message["bs"]),
            ask_price=float(message["ap"]),
            ask_size=float(message["as"]),
            raw=message,
        )
    if message_type == "t":
        trade_id = message.get("i")
        return MarketTrade(
            symbol=str(message["S"]),
            timestamp=str(message["t"]),
            price=float(message["p"]),
            size=float(message["s"]),
            trade_id=int(trade_id) if trade_id is not None else None,
            raw=message,
        )
    return None


def parse_order_update(message: dict[str, Any]) -> OrderUpdate | None:
    """Convert an Alpaca ``trade_updates`` message into a typed event."""
    if message.get("stream") != "trade_updates":
        return None
    data = message.get("data") or {}
    order = data.get("order") or {}
    event = str(data.get("event", ""))
    if not event or not order:
        return None
    return OrderUpdate(
        event=event,
        order_id=str(order.get("id", "")),
        client_order_id=str(order.get("client_order_id", "")),
        symbol=str(order.get("symbol", "")),
        side=str(order.get("side", "")),
        status=str(order.get("status", "")),
        qty=str(order.get("qty", "")),
        filled_qty=str(order.get("filled_qty", "0")),
        raw=message,
    )


def _frames(raw: str | bytes) -> list[dict[str, Any]]:
    messages = json.loads(raw)
    return messages if isinstance(messages, list) else [messages]


class _AlpacaSocket:
    """Shared transport. The handshake is NOT shared — see the module docstring."""

    def __init__(self, settings: Settings, url: str):
        self.settings = settings
        self.url = url
        # Frames read while looking for a different one. A handshake step must
        # not destroy a frame a later step needs: waiting for the greeting and
        # discarding non-matches ate the auth reply on endpoints that send no
        # greeting.
        self._buffer: list[dict[str, Any]] = []

    def connect(self):
        return websockets.connect(
            self.url,
            open_timeout=10,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=20,
            max_size=1_000_000,
        )

    def _check(self, message: dict[str, Any], describe: str, is_error) -> None:
        if is_error is not None and is_error(message):
            raise RuntimeError(f"Alpaca refused {describe}: {message}")
        if message.get("T") == "error":
            raise RuntimeError(f"Alpaca refused {describe}: {message}")

    async def _await_frame(self, websocket: Any, matches, describe: str,
                           is_error=None,
                           timeout: float = _HANDSHAKE_TIMEOUT_S) -> dict[str, Any]:
        """Scan for the frame this handshake step expects, keeping the rest.

        Alpaca interleaves greetings and unrelated frames with handshake
        replies, so reading exactly one frame is not enough. Frames that do not
        match are BUFFERED rather than dropped, because the next step may need
        one — waiting for the greeting and discarding non-matches consumed the
        auth reply outright when no greeting was sent.

        Bounded by frame count and timeout, so a stalled or noisy server
        surfaces as a clean error instead of a hang.
        """
        for index, message in enumerate(self._buffer):
            self._check(message, describe, is_error)
            if matches(message):
                del self._buffer[index]
                return message

        for _ in range(_HANDSHAKE_FRAME_LIMIT):
            raw = await asyncio.wait_for(websocket.recv(), timeout=timeout)
            for message in _frames(raw):
                self._check(message, describe, is_error)
                if matches(message):
                    return message
                logger.debug("buffering frame while awaiting %s: %s", describe, message)
                self._buffer.append(message)
        raise RuntimeError(
            f"no {describe} response from Alpaca within "
            f"{_HANDSHAKE_FRAME_LIMIT} frames")


class _MarketDataSocket(_AlpacaSocket):
    """wss://stream.data.alpaca.markets/v2/<feed>"""

    async def authenticate(self, websocket: Any) -> None:
        # Wait for the greeting BEFORE sending auth. Alpaca opens with
        # {"T":"success","msg":"connected"} and an auth frame sent ahead of it
        # is sometimes ignored — the socket then sits open, silent, until the
        # handshake times out and reconnects, forever.
        #
        # This was intermittent, which is worse than broken: three runs
        # connected and delivered ticks, the fourth hung. Sending first and
        # skipping the greeting afterwards happens to work whenever the
        # greeting arrives before the server processes the auth, and that is a
        # race, not a design.
        # Only a timeout is tolerated. An error frame here is a real refusal
        # — connection limit, bad endpoint — and swallowing it turned an
        # explicit rejection into a silent reconnect loop.
        with contextlib.suppress(asyncio.TimeoutError):
            await self._await_frame(
                websocket,
                lambda m: m.get("T") == "success" and m.get("msg") == "connected",
                "connection greeting", timeout=_GREETING_TIMEOUT_S)

        await websocket.send(json.dumps({
            "action": "auth",
            "key": self.settings.alpaca_key_id,
            "secret": self.settings.alpaca_secret_key,
        }))
        await self._await_frame(
            websocket,
            lambda m: m.get("T") == "success" and m.get("msg") == "authenticated",
            "authentication")

    async def subscribe(self, websocket: Any, symbols: list[str]) -> None:
        if not symbols:
            return
        await websocket.send(json.dumps({
            "action": "subscribe", "quotes": symbols, "trades": symbols,
        }))
        await self._await_frame(
            websocket, lambda m: m.get("T") == "subscription", "subscription")


class _TradingSocket(_AlpacaSocket):
    """wss://paper-api.alpaca.markets/stream — a different protocol entirely."""

    @staticmethod
    def _unauthorized(message: dict[str, Any]) -> bool:
        return (message.get("stream") == "authorization"
                and (message.get("data") or {}).get("status") == "unauthorized")

    async def authenticate(self, websocket: Any) -> None:
        await websocket.send(json.dumps({
            "action": "authenticate",
            "data": {"key_id": self.settings.alpaca_key_id,
                     "secret_key": self.settings.alpaca_secret_key},
        }))
        await self._await_frame(
            websocket,
            lambda m: (m.get("stream") == "authorization"
                       and (m.get("data") or {}).get("status") == "authorized"),
            "authorization", is_error=self._unauthorized)

    async def listen(self, websocket: Any, streams: list[str]) -> None:
        await websocket.send(json.dumps({
            "action": "listen", "data": {"streams": streams},
        }))
        await self._await_frame(
            websocket, lambda m: m.get("stream") == "listening", "listen")


class AlpacaMarketStream(_MarketDataSocket):
    def __init__(self, settings: Settings, on_quote: Callback | None = None,
                 on_trade: Callback | None = None, on_error: Callback | None = None,
                 url: str | None = None, symbols: tuple[str, ...] | None = None,
                 label: str = "market"):
        super().__init__(settings, url or settings.market_stream_url)
        self.on_quote = on_quote
        self.on_trade = on_trade
        self.on_error = on_error
        # Crypto is a separate endpoint with its own symbol list, not another
        # feed of the equity socket — so the URL and symbols are injectable
        # rather than read from one hardcoded pair of settings.
        self._symbols = symbols
        self.label = label

    @property
    def symbols(self) -> list[str]:
        return list(self._symbols if self._symbols is not None
                    else self.settings.market_symbols)

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        await _run_with_reconnect(self._run_once, stop_event, self.on_error, self.label)

    async def _run_once(self) -> None:
        self._buffer.clear()          # frames never survive a reconnect
        async with self.connect() as websocket:
            await self.authenticate(websocket)
            await self.subscribe(websocket, self.symbols)
            async for raw in websocket:
                for message in _frames(raw):
                    event = parse_market_message(message)
                    if isinstance(event, MarketQuote):
                        await _invoke(self.on_quote, event)
                    elif isinstance(event, MarketTrade):
                        await _invoke(self.on_trade, event)


class AlpacaTradeUpdateStream(_TradingSocket):
    def __init__(self, settings: Settings, on_update: Callback | None = None,
                 on_error: Callback | None = None,
                 on_connected: Callable[[], Awaitable[None]] | None = None):
        super().__init__(settings, settings.trade_stream_url)
        self.on_update = on_update
        self.on_error = on_error
        # Called after every successful (re)connection. Alpaca does not replay
        # trade_updates missed while disconnected, so without this every
        # reconnect is a hole where fills vanish permanently — the store would
        # believe a position is flat when it is not.
        self.on_connected = on_connected

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        await _run_with_reconnect(self._run_once, stop_event, self.on_error,
                                  "trade_updates")

    async def _run_once(self) -> None:
        self._buffer.clear()          # frames never survive a reconnect
        async with self.connect() as websocket:
            await self.authenticate(websocket)
            await self.listen(websocket, ["trade_updates"])
            if self.on_connected is not None:
                # Resync BEFORE reading, so anything missed during the outage is
                # recovered even if no new update ever arrives.
                await self.on_connected()
            async for raw in websocket:
                for message in _frames(raw):
                    event = parse_order_update(message)
                    if event is not None:
                        await _invoke(self.on_update, event)


async def _run_with_reconnect(run_once: Callable[[], Awaitable[None]],
                              stop_event: asyncio.Event,
                              on_error: Callback | None, stream_name: str) -> None:
    delay = 1.0
    loop = asyncio.get_running_loop()
    while not stop_event.is_set():
        started = loop.time()
        try:
            await run_once()
            # Returning normally means the server closed the socket cleanly.
            # That is a disconnect, not a success.
            logger.info("Alpaca %s stream closed by server", stream_name)
        except asyncio.CancelledError:
            raise
        except (ConnectionClosed, OSError, RuntimeError, ValueError,
                asyncio.TimeoutError, json.JSONDecodeError) as exc:
            logger.warning("Alpaca %s stream disconnected: %s", stream_name, exc)
            await _invoke(on_error, exc)
        except Exception as exc:  # keep the persistent stream alive, but surface it
            logger.exception("unexpected Alpaca %s stream failure", stream_name)
            await _invoke(on_error, exc)

        # Only a connection that stayed up earns a reset. Resetting on every
        # attempt turns a server that closes immediately into a 1s hot loop.
        if loop.time() - started >= _STABLE_CONNECTION_S:
            delay = 1.0
        if not stop_event.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            delay = min(delay * 2, 30.0)


class AlpacaStreamManager:
    """Own both persistent Alpaca streams and stop them as one unit."""

    def __init__(self, settings: Settings, on_quote: Callback | None = None,
                 on_trade: Callback | None = None,
                 on_order_update: Callback | None = None,
                 on_error: Callback | None = None,
                 on_order_stream_connected: Callable[[], Awaitable[None]] | None = None):
        self.settings = settings
        self.stop_event = asyncio.Event()
        self.tasks: list[asyncio.Task[None]] = []
        # One socket per asset class, because they are different endpoints.
        # Each is created only when it has symbols — an empty subscribe list
        # would hold a socket open receiving nothing.
        self.market = (
            AlpacaMarketStream(settings, on_quote, on_trade, on_error,
                               symbols=settings.equity_stream_symbols)
            if settings.equity_stream_symbols else None
        )
        self.crypto = (
            AlpacaMarketStream(settings, on_quote, on_trade, on_error,
                               url=settings.crypto_stream_url,
                               symbols=settings.crypto_stream_symbols,
                               label="crypto")
            if settings.crypto_stream_symbols else None
        )
        self.trade_updates = AlpacaTradeUpdateStream(
            settings, on_order_update, on_error, on_order_stream_connected)

    async def start(self) -> None:
        if self.tasks:
            return
        if not self.settings.alpaca_key_id or not self.settings.alpaca_secret_key:
            raise RuntimeError("Alpaca credentials are required to start streaming")
        self.stop_event.clear()
        self.tasks = [
            asyncio.create_task(self.trade_updates.run_forever(self.stop_event),
                                name="alpaca-trade-updates-stream"),
        ]
        for stream, name in ((self.market, "alpaca-market-stream"),
                             (self.crypto, "alpaca-crypto-stream")):
            if stream is not None:
                self.tasks.append(
                    asyncio.create_task(stream.run_forever(self.stop_event), name=name))

    async def stop(self, timeout: float = 5.0) -> None:
        """Stop both streams, and do not hang if they will not stop politely.

        The stop event is only observed between reconnect attempts. A task
        parked in `async for raw in websocket` never looks at it, so a HEALTHY
        stream — the normal case — ignored it completely and awaiting the tasks
        blocked forever, hanging application shutdown. The event gives each task
        a chance to exit cleanly; cancellation is what guarantees it does.
        """
        self.stop_event.set()
        tasks, self.tasks = self.tasks, []
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            logger.warning("force-cancelling %s: did not stop within %.1fs",
                           task.get_name(), timeout)
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if task.cancelled():
                continue
            if (exc := task.exception()) is not None:
                logger.warning("%s ended with %r", task.get_name(), exc)
