from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation

from . import assets
from .assets import is_crypto

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

    def submit_order(self, **kwargs) -> dict:
        """Submit the keyword-based order shape used by Stage 3 execution."""
        if not self.settings.alpaca_key_id or not self.settings.alpaca_secret_key:
            raise RuntimeError("Alpaca paper credentials are not configured")
        payload = dict(kwargs)
        if "qty" in payload:
            payload["qty"] = assets.format_qty(payload["qty"])
        for key in ("limit_price", "stop_price", "trail_price"):
            if key in payload and payload[key] is not None:
                payload[key] = str(payload[key])
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
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"Alpaca rejected order: HTTP {exc.code}: {detail}") from exc

    def cancel_order(self, order_id: str) -> None:
        request = urllib.request.Request(
            f"{self.settings.alpaca_base_url.rstrip('/')}/v2/orders/{order_id}",
            headers={
                "APCA-API-KEY-ID": self.settings.alpaca_key_id,
                "APCA-API-SECRET-KEY": self.settings.alpaca_secret_key,
            },
            method="DELETE",
        )
        try:
            with urllib.request.urlopen(request, timeout=5):
                return
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"Alpaca order cancellation failed: HTTP {exc.code}: {detail}") from exc

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

    def position_qty(self, symbol: str) -> Decimal:
        """How much of `symbol` is actually held.

        The execution engine sizes protection from this, never from the fill,
        because Alpaca charges the crypto fee in kind: a filled 0.001 BTC
        leaves a position of 0.0009975, and a stop sized to the fill asks to
        sell more than is held and is refused.

        A symbol with no position returns 0 rather than raising — Alpaca
        answers 404 for a flat symbol, which is an answer, not a failure.

        The positions endpoint wants the SLASHLESS spelling, unlike the crypto
        data endpoints which require the slash:

            /v2/positions/BTC%2FUSD  -> 404
            /v2/positions/BTCUSD     -> 200, qty 0.00348875

        Sending the slash returns 404, and 404 means flat — so a held position
        reads as zero, the engine concludes there is nothing to protect, and a
        filled position is left without a stop. The wrong URL does not fail; it
        produces a plausible answer, which is worse.
        """
        url = (f"{self.settings.alpaca_base_url.rstrip('/')}/v2/positions/"
               + urllib.parse.quote(symbol.replace("/", ""), safe=""))
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
            if exc.code == 404:
                return Decimal("0")
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(
                f"Alpaca position lookup failed: HTTP {exc.code}: {detail}") from exc
        try:
            return Decimal(str(raw["qty"]))
        except (KeyError, TypeError, InvalidOperation) as exc:
            raise RuntimeError(
                f"Alpaca position response had no usable qty for {symbol}") from exc

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

    def _trading_get(self, path: str) -> Any:
        """GET against the trading API. 404 comes back as None.

        A 404 here is an answer — no such order — and the callers below all
        need to tell "not found" apart from "the lookup failed", because
        treating a failed lookup as "not found" is how a live order becomes
        invisible to reconciliation.
        """
        request = urllib.request.Request(
            f"{self.settings.alpaca_base_url.rstrip('/')}{path}",
            headers={
                "APCA-API-KEY-ID": self.settings.alpaca_key_id,
                "APCA-API-SECRET-KEY": self.settings.alpaca_secret_key,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(
                f"Alpaca GET {path} failed: HTTP {exc.code}: {detail}") from exc

    def get_order_by_client_id(self, client_order_id: str) -> dict | None:
        """One order by the id we chose for it.

        Reconciliation identifies orders by client id rather than broker id
        because the client id is the only one that survives a crash before the
        response came back — which is exactly the case reconciliation exists
        for.
        """
        return self._trading_get(
            "/v2/orders:by_client_order_id?client_order_id="
            + urllib.parse.quote(client_order_id, safe=""))

    def open_orders(self, symbol: str) -> list[dict]:
        """Live orders for one symbol.

        The orders endpoint wants the SLASHED spelling for crypto, unlike
        /v2/positions which wants it slashless. Both silently return something
        plausible for the wrong form — an empty list here, a 404 there — so the
        two are written out separately rather than sharing a helper that would
        have to guess.
        """
        return self._trading_get(
            "/v2/orders?status=open&symbols=" + urllib.parse.quote(symbol, safe="")
        ) or []

    def min_order_size(self, symbol: str) -> Decimal:
        """The smallest quantity Alpaca will accept for this asset.

        Checked when an exit plan is built, so a ladder whose smallest tranche
        is unplaceable is refused before the first order rather than stalling
        halfway down with a remainder it can never sell. BTC/USD is currently
        0.000015417; equities are whole shares.
        """
        if not is_crypto(symbol):
            return Decimal("1")
        asset = self._trading_get(
            "/v2/assets/" + urllib.parse.quote(symbol, safe="")) or {}
        return Decimal(str(asset.get("min_order_size") or "0.000000001"))

    def fill_price(self, order_id: str) -> Decimal | None:
        """What an order actually filled at.

        R is entry minus stop, and a market order into a fast tape does not
        fill at the price the signal was generated from. Sizing a ladder off
        the signal price puts every target at the wrong distance from the risk
        actually taken.
        """
        raw = self._trading_get(f"/v2/orders/{urllib.parse.quote(order_id, safe='')}")
        price = (raw or {}).get("filled_avg_price")
        return Decimal(str(price)) if price else None

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
    """Mirrors AlpacaPaperClient's surface.

    Kept in step deliberately: when the fakes and the real client disagree,
    tests pass against an interface nothing implements — which is how a 502
    reached the first genuine paper order.
    """

    def __init__(self):
        self.orders: list[dict] = []
        self.canceled: list[str] = []
        self.positions: dict[str, Decimal] = {}
        self.resting: dict[str, dict] = {}

    def submit_order(self, **kwargs) -> dict:
        order_id = f"fake-{len(self.orders) + 1}"
        self.orders.append({**kwargs, "id": order_id})
        qty = Decimal(str(kwargs.get("qty", 0)))
        symbol = kwargs.get("symbol", "")
        resting = kwargs.get("type") in {"stop_limit", "stop", "trailing_stop"}
        if not resting:
            held = self.positions.get(symbol, Decimal("0"))
            sign = 1 if kwargs.get("side") == "buy" else -1
            self.positions[symbol] = held + sign * qty
        if resting:
            self.resting[order_id] = {**kwargs, "id": order_id}
        return {"id": order_id, "status": "new" if resting else "filled",
                "filled_qty": "0" if resting else str(qty)}

    def cancel_order(self, order_id: str) -> None:
        self.canceled.append(order_id)
        self.resting.pop(order_id, None)

    def position_qty(self, symbol: str) -> Decimal:
        return self.positions.get(symbol, Decimal("0"))

    def get_order_by_client_id(self, client_order_id: str) -> dict | None:
        for order in self.orders:
            if order.get("client_order_id") == client_order_id:
                return order
        return None

    def open_orders(self, symbol: str) -> list[dict]:
        return [o for oid, o in self.resting.items() if o.get("symbol") == symbol]

    def min_order_size(self, symbol: str) -> Decimal:
        # The real BTC/USD figure, so a fake ladder is sized against the same
        # floor the account enforces.
        return Decimal("0.000015417") if is_crypto(symbol) else Decimal("1")

    def fill_price(self, order_id: str) -> Decimal | None:
        for order in self.orders:
            if order.get("id") == order_id:
                price = order.get("filled_avg_price")
                return Decimal(str(price)) if price else None
        return None

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

    def position_qty(self, symbol: str) -> Decimal:
        """How much of `symbol` is actually held.

        The execution engine sizes protection from this, never from the fill,
        because Alpaca charges the crypto fee in kind: a filled 0.001 BTC
        leaves a position of 0.0009975, and a stop sized to the fill asks to
        sell more than is held and is refused.

        A symbol with no position returns 0 rather than raising — Alpaca
        answers 404 for a flat symbol, which is an answer, not a failure.

        The positions endpoint wants the SLASHLESS spelling, unlike the crypto
        data endpoints which require the slash:

            /v2/positions/BTC%2FUSD  -> 404
            /v2/positions/BTCUSD     -> 200, qty 0.00348875

        Sending the slash returns 404, and 404 means flat — so a held position
        reads as zero, the engine concludes there is nothing to protect, and a
        filled position is left without a stop. The wrong URL does not fail; it
        produces a plausible answer, which is worse.
        """
        url = (f"{self.settings.alpaca_base_url.rstrip('/')}/v2/positions/"
               + urllib.parse.quote(symbol.replace("/", ""), safe=""))
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
            if exc.code == 404:
                return Decimal("0")
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(
                f"Alpaca position lookup failed: HTTP {exc.code}: {detail}") from exc
        try:
            return Decimal(str(raw["qty"]))
        except (KeyError, TypeError, InvalidOperation) as exc:
            raise RuntimeError(
                f"Alpaca position response had no usable qty for {symbol}") from exc

    def get_order(self, order_id: str) -> BrokerResult:
        for raw in self.orders:
            if raw["id"] == order_id:
                return BrokerResult(order_id=order_id, status="accepted", raw=raw)
        raise RuntimeError("fake order not found")

    def latest_trade_price(self, symbol: str) -> float:
        return 64_900.0 if assets.is_crypto(symbol) else 700.0
