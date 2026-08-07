from __future__ import annotations

import asyncio
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


class _AlpacaSocket:
    def __init__(self, settings: Settings, url: str, streams: list[str]):
        self.settings = settings
        self.url = url
        self.streams = streams

    def connect(self):
        return websockets.connect(
            self.url,
            open_timeout=10,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=20,
            max_size=1_000_000,
        )

    async def authenticate(self, websocket: Any) -> None:
        await websocket.send(
            json.dumps(
                {
                    "action": "auth",
                    "key": self.settings.alpaca_key_id,
                    "secret": self.settings.alpaca_secret_key,
                }
            )
        )
        await self._expect_success(websocket, "authenticated")
        await websocket.send(json.dumps({"action": "listen", "data": {"streams": self.streams}}))
        await self._expect_success(websocket, "subscribed")

    @staticmethod
    async def _expect_success(websocket: Any, operation: str) -> None:
        raw = await websocket.recv()
        messages = json.loads(raw)
        if not isinstance(messages, list):
            messages = [messages]
        for message in messages:
            if message.get("T") == "error":
                raise RuntimeError(f"Alpaca stream {operation} failed: {message}")
            if message.get("T") == "success" and message.get("msg") in {
                "authenticated",
                "listening",
            }:
                return
        raise RuntimeError(f"unexpected Alpaca stream response during {operation}: {messages}")


class AlpacaMarketStream(_AlpacaSocket):
    def __init__(self, settings: Settings, on_quote: Callback | None = None, on_trade: Callback | None = None, on_error: Callback | None = None):
        super().__init__(settings, settings.market_stream_url, ["quotes", "trades"])
        self.on_quote = on_quote
        self.on_trade = on_trade
        self.on_error = on_error

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        await _run_with_reconnect(self._run_once, stop_event, self.on_error, "market")

    async def _run_once(self) -> None:
        async with self.connect() as websocket:
            await self.authenticate(websocket)
            if self.settings.market_symbols:
                await websocket.send(
                    json.dumps(
                        {
                            "action": "subscribe",
                            "quotes": list(self.settings.market_symbols),
                            "trades": list(self.settings.market_symbols),
                        }
                    )
                )
            async for raw in websocket:
                messages = json.loads(raw)
                if not isinstance(messages, list):
                    messages = [messages]
                for message in messages:
                    event = parse_market_message(message)
                    if isinstance(event, MarketQuote):
                        await _invoke(self.on_quote, event)
                    elif isinstance(event, MarketTrade):
                        await _invoke(self.on_trade, event)


class AlpacaTradeUpdateStream(_AlpacaSocket):
    def __init__(self, settings: Settings, on_update: Callback | None = None, on_error: Callback | None = None):
        super().__init__(settings, settings.trade_stream_url, ["trade_updates"])
        self.on_update = on_update
        self.on_error = on_error

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        await _run_with_reconnect(self._run_once, stop_event, self.on_error, "trade_updates")

    async def _run_once(self) -> None:
        async with self.connect() as websocket:
            await self.authenticate(websocket)
            async for raw in websocket:
                messages = json.loads(raw)
                if not isinstance(messages, list):
                    messages = [messages]
                for message in messages:
                    event = parse_order_update(message)
                    if event is not None:
                        await _invoke(self.on_update, event)


async def _run_with_reconnect(run_once: Callable[[], Awaitable[None]], stop_event: asyncio.Event, on_error: Callback | None, stream_name: str) -> None:
    delay = 1.0
    while not stop_event.is_set():
        try:
            await run_once()
            delay = 1.0
        except asyncio.CancelledError:
            raise
        except (ConnectionClosed, OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Alpaca %s stream disconnected: %s", stream_name, exc)
            await _invoke(on_error, exc)
        except Exception as exc:  # keep the persistent stream alive, but surface the failure
            logger.exception("unexpected Alpaca %s stream failure", stream_name)
            await _invoke(on_error, exc)
        if not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, 30.0)


class AlpacaStreamManager:
    """Own both persistent Alpaca streams and stop them as one unit."""

    def __init__(self, settings: Settings, on_quote: Callback | None = None, on_trade: Callback | None = None, on_order_update: Callback | None = None, on_error: Callback | None = None):
        self.settings = settings
        self.stop_event = asyncio.Event()
        self.tasks: list[asyncio.Task[None]] = []
        self.market = AlpacaMarketStream(settings, on_quote, on_trade, on_error)
        self.trade_updates = AlpacaTradeUpdateStream(settings, on_order_update, on_error)

    async def start(self) -> None:
        if self.tasks:
            return
        if not self.settings.alpaca_key_id or not self.settings.alpaca_secret_key:
            raise RuntimeError("Alpaca credentials are required to start streaming")
        self.stop_event.clear()
        self.tasks = [
            asyncio.create_task(self.market.run_forever(self.stop_event), name="alpaca-market-stream"),
            asyncio.create_task(self.trade_updates.run_forever(self.stop_event), name="alpaca-trade-updates-stream"),
        ]

    async def stop(self) -> None:
        self.stop_event.set()
        tasks, self.tasks = self.tasks, []
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
