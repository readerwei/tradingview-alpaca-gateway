from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from decimal import Decimal

from . import assets

from .config import Settings
from .risk import ApprovedOrder


@dataclass(frozen=True)
class BrokerResult:
    order_id: str
    status: str
    raw: dict


class AlpacaPaperClient:
    def __init__(self, settings: Settings):
        settings.validate()
        self.settings = settings

    def submit(self, order: ApprovedOrder, client_order_id: str) -> BrokerResult:
        if not self.settings.alpaca_key_id or not self.settings.alpaca_secret_key:
            raise RuntimeError("Alpaca paper credentials are not configured")
        payload = {
            "symbol": order.symbol,
            "qty": assets.format_qty(order.qty),
            "side": order.side,
            "type": "limit",
            "time_in_force": order.time_in_force,
            "limit_price": f"{order.limit_price:.4f}",
            "extended_hours": order.extended_hours,
            "client_order_id": client_order_id,
        }
        request = urllib.request.Request(
            f"{self.settings.alpaca_base_url.rstrip('/')}/v2/orders",
            data=json.dumps(payload).encode(),
            headers={
                "content-type": "application/json",
                "APCA-API-KEY-ID": self.settings.alpaca_key_id,
                "APCA-API-SECRET-KEY": self.settings.alpaca_secret_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"Alpaca rejected order: HTTP {exc.code}: {detail}") from exc
        return BrokerResult(order_id=raw.get("id", ""), status=raw.get("status", "unknown"), raw=raw)
    def _latest_crypto_price(self, symbol: str) -> float:
        url = ("https://data.alpaca.markets/v1beta3/crypto/us/latest/trades?symbols="
               + urllib.parse.quote(symbol, safe=""))
        request = urllib.request.Request(
            url,
            headers={
                "APCA-API-KEY-ID": self.settings.alpaca_key_id,
                "APCA-API-SECRET-KEY": self.settings.alpaca_secret_key,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(
                f"Alpaca crypto market-data lookup failed: HTTP {exc.code}: {detail}") from exc
        try:
            price = float(raw["trades"][symbol]["p"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Alpaca crypto market-data response had no trade price for {symbol}") from exc
        if price <= 0:
            raise RuntimeError("Alpaca crypto market-data returned a non-positive price")
        return price

    def get_order(self, order_id: str) -> BrokerResult:
        request = urllib.request.Request(
            f"{self.settings.alpaca_base_url.rstrip('/')}/v2/orders/{order_id}",
            headers={
                "APCA-API-KEY-ID": self.settings.alpaca_key_id,
                "APCA-API-SECRET-KEY": self.settings.alpaca_secret_key,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"Alpaca order lookup failed: HTTP {exc.code}: {detail}") from exc
        return BrokerResult(order_id=raw.get("id", order_id), status=raw.get("status", "unknown"), raw=raw)

    def latest_trade_price(self, symbol: str) -> float:
        # Crypto lives on a different endpoint with a different response shape.
        # Asking the equity endpoint for BTC/USD does not fail loudly, it just
        # never returns a price — and the collar then refuses every crypto
        # alert for "market data unavailable".
        if assets.is_crypto(symbol):
            return self._latest_crypto_price(symbol)
        request = urllib.request.Request(
            f"https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest?feed={self.settings.market_data_feed}",
            headers={
                "APCA-API-KEY-ID": self.settings.alpaca_key_id,
                "APCA-API-SECRET-KEY": self.settings.alpaca_secret_key,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"Alpaca market-data lookup failed: HTTP {exc.code}: {detail}") from exc
        try:
            price = float(raw["trade"]["p"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Alpaca market-data response did not contain a valid trade price") from exc
        if price <= 0:
            raise RuntimeError("Alpaca market-data response contained a non-positive trade price")
        return price


class FakeBroker:
    def __init__(self):
        self.orders: list[dict] = []

    def submit(self, order: ApprovedOrder, client_order_id: str) -> BrokerResult:
        raw = {"id": f"fake-{len(self.orders) + 1}", "client_order_id": client_order_id, **asdict(order)}
        self.orders.append(raw)
        return BrokerResult(order_id=raw["id"], status="accepted", raw=raw)

    def _latest_crypto_price(self, symbol: str) -> float:
        url = ("https://data.alpaca.markets/v1beta3/crypto/us/latest/trades?symbols="
               + urllib.parse.quote(symbol, safe=""))
        request = urllib.request.Request(
            url,
            headers={
                "APCA-API-KEY-ID": self.settings.alpaca_key_id,
                "APCA-API-SECRET-KEY": self.settings.alpaca_secret_key,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(
                f"Alpaca crypto market-data lookup failed: HTTP {exc.code}: {detail}") from exc
        try:
            price = float(raw["trades"][symbol]["p"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Alpaca crypto market-data response had no trade price for {symbol}") from exc
        if price <= 0:
            raise RuntimeError("Alpaca crypto market-data returned a non-positive price")
        return price

    def get_order(self, order_id: str) -> BrokerResult:
        for raw in self.orders:
            if raw["id"] == order_id:
                return BrokerResult(order_id=order_id, status="accepted", raw=raw)
        raise RuntimeError("fake order not found")

    def latest_trade_price(self, symbol: str) -> float:
        return 64_900.0 if assets.is_crypto(symbol) else 700.0
