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
    """Alpaca's own SDK, behind the method surface the gateway already calls.

    Wei: "wherever you can, you should reuse existing SDK, not reinvent wheel
    yourself." So auth, retries, JSON, error shapes and the order models are
    alpaca-py's problem now.

    The surface stays ours deliberately. Every one of the 270 tests constrains
    these method names and their behaviour, and swapping the interface as well
    would leave the contract suite testing an interface nothing implements —
    which is the exact defect that let a 502 reach the first real order.

    ONE THING THE SDK DOES NOT DO
    -----------------------------
    It does not normalise crypto symbols. From its own source:

        symbol_or_asset_id = validate_symbol_or_asset_id(symbol_or_asset_id)
        response = self.get(f"/positions/{symbol_or_asset_id}")

    `validate_symbol_or_asset_id` checks the TYPE and returns the string
    untouched, and nothing in the client URL-encodes. So `BTC/USD` interpolates
    a raw slash and requests `/positions/BTC/USD` — a different route. Our
    version of that bug returned 404, which reads as flat, which left a filled
    position with no stop. The normalisation therefore stays here, and stays
    tested.
    """

    def __init__(self, settings: Settings):
        settings.validate()
        self.settings = settings
        self._client: Any = None

    @property
    def client(self) -> Any:
        # Built on first use so constructing the gateway never needs the network
        # and never needs real credentials.
        if self._client is None:
            from alpaca.trading.client import TradingClient
            self._client = TradingClient(
                self.settings.alpaca_key_id, self.settings.alpaca_secret_key,
                paper=True)
        return self._client

    @staticmethod
    def _as_dict(order: Any) -> dict:
        """SDK model -> the plain dict the engine reads.

        Values are stringified because everything downstream parses them into
        Decimal. Handing back a float here would put a binary rounding error
        into an order quantity.
        """
        if order is None:
            return {}
        raw = order.model_dump() if hasattr(order, "model_dump") else dict(order)
        # Unwrap enums BEFORE stringifying. The SDK returns OrderSide.SELL and
        # OrderType.STOP_LIMIT, and str() on those gives "OrderSide.SELL" —
        # which silently fails every `== "sell"` comparison in the exit
        # manager, so a resting stop stops being recognised as a resting sell.
        # Caught by running against the live account; the spy returned plain
        # strings and the suite was green.
        out = {k: (str(getattr(v, "value", v)) if v is not None else None)
               for k, v in raw.items()}
        out["id"] = str(raw.get("id", ""))
        out["status"] = out.get("status") or "unknown"
        out["filled_qty"] = str(raw.get("filled_qty") or "0")
        return out

    def _order_request(self, **kwargs: Any) -> Any:
        from alpaca.trading import requests as rq
        from alpaca.trading.enums import OrderSide, TimeInForce

        shared = dict(
            symbol=kwargs["symbol"],
            qty=float(assets.format_qty(Decimal(str(kwargs["qty"])))),
            side=OrderSide(str(kwargs["side"]).lower()),
            time_in_force=TimeInForce(str(kwargs.get("time_in_force", "gtc")).lower()),
            client_order_id=kwargs.get("client_order_id"),
        )
        order_type = str(kwargs.get("type", "market")).lower()
        if order_type == "market":
            return rq.MarketOrderRequest(**shared)
        if order_type == "limit":
            return rq.LimitOrderRequest(limit_price=float(kwargs["limit_price"]), **shared)
        if order_type == "stop":
            return rq.StopOrderRequest(stop_price=float(kwargs["stop_price"]), **shared)
        if order_type == "stop_limit":
            return rq.StopLimitOrderRequest(
                stop_price=float(kwargs["stop_price"]),
                limit_price=float(kwargs["limit_price"]), **shared)
        if order_type == "trailing_stop":
            return rq.TrailingStopOrderRequest(
                trail_percent=float(kwargs["trail_percent"]), **shared)
        raise RuntimeError(f"unsupported order type {order_type!r}")

    def submit(self, order: ApprovedOrder, client_order_id: str) -> BrokerResult:
        raw = self.submit_order(
            symbol=order.symbol, qty=order.qty, side=order.side, type="limit",
            time_in_force=order.time_in_force, limit_price=order.limit_price,
            client_order_id=client_order_id)
        return BrokerResult(order_id=raw.get("id", ""),
                            status=raw.get("status", "unknown"), raw=raw)

    def submit_order(self, **kwargs: Any) -> dict:
        if not self.settings.alpaca_key_id or not self.settings.alpaca_secret_key:
            raise RuntimeError("Alpaca paper credentials are not configured")
        return self._as_dict(self.client.submit_order(self._order_request(**kwargs)))

    def cancel_order(self, order_id: str) -> None:
        self.client.cancel_order_by_id(order_id)

    def replace_order(self, order_id: str, qty: Decimal) -> dict:
        """Resize a resting order in one operation.

        The SDK has this and the hand-rolled client did not. Whether Alpaca
        performs it atomically on crypto is unverified — until it is, the exit
        manager still cancels and re-places, which is what Wei chose.
        """
        from alpaca.trading.requests import ReplaceOrderRequest
        return self._as_dict(self.client.replace_order_by_id(
            order_id, ReplaceOrderRequest(qty=int(qty) if qty == qty.to_integral_value()
                                          else float(qty))))

    def get_order(self, order_id: str) -> BrokerResult:
        raw = self._as_dict(self.client.get_order_by_id(order_id))
        return BrokerResult(order_id=raw.get("id", order_id),
                            status=raw.get("status", "unknown"), raw=raw)

    def get_order_by_client_id(self, client_order_id: str) -> dict | None:
        """404 comes back as None — no such order is an answer, not a failure."""
        try:
            return self._as_dict(self.client.get_order_by_client_id(client_order_id))
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise

    def open_orders(self, symbol: str) -> list[dict]:
        """Live orders for one symbol, in the SLASHED spelling.

        Verified against the live account: `symbols=BTCUSD` returns an empty
        list with HTTP 200 while `BTC%2FUSD` returns the resting stop. An empty
        list is what reconciliation reads as "the stop is gone", so it would
        cancel nothing and place a duplicate. The opposite convention from
        /v2/positions, and neither mistake shows in the response.
        """
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = self.client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.OPEN, symbols=[assets.normalise(symbol)]))
        return [self._as_dict(order) for order in orders or []]

    def min_order_size(self, symbol: str) -> Decimal:
        """Asked for, never hardcoded: Alpaca recalculates it against price, and
        BTC/USD moved 0.000015417 -> 0.000015437 in a few hours."""
        if not is_crypto(symbol):
            return Decimal("1")
        asset = self.client.get_asset(assets.normalise(symbol))
        return Decimal(str(getattr(asset, "min_order_size", None) or "0.000000001"))

    def fill_price(self, order_id: str) -> Decimal | None:
        """What an order actually filled at.

        R is entry minus stop, and a market order into a fast tape does not fill
        where the signal fired.
        """
        price = self._as_dict(self.client.get_order_by_id(order_id)).get(
            "filled_avg_price")
        return Decimal(str(price)) if price else None

    def recent_bars(self, symbol: str, timeframe: str, limit: int = 30) -> list:
        """Completed bars, newest last, for seeding a runner's trail at startup.

        Without this a gateway that restarts mid-position trails nothing until
        the first live bar arrives — up to a minute on 1m, and indefinitely if
        the single market-data connection slot is held by another process. The
        stop would sit at its last persisted value while price moved.

        The forming bar is excluded by its own timestamp rather than by
        dropping the newest row, because whether Alpaca includes it varies and
        a positional rule would silently discard a completed bar on the runs
        where it does not.
        """
        from datetime import datetime, timedelta, timezone

        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        amount, unit = _parse_timeframe(timeframe)
        period = timedelta(**{{"Min": "minutes", "Hour": "hours",
                               "Day": "days"}[unit.value]: amount})
        start = datetime.now(timezone.utc) - period * (limit + 2)
        frame = TimeFrame(amount, unit)

        if is_crypto(symbol):
            from alpaca.data.historical import CryptoHistoricalDataClient
            from alpaca.data.requests import CryptoBarsRequest
            client = CryptoHistoricalDataClient(self.settings.alpaca_key_id,
                                                self.settings.alpaca_secret_key)
            bars = client.get_crypto_bars(CryptoBarsRequest(
                symbol_or_symbols=assets.normalise(symbol), timeframe=frame,
                start=start))
        else:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockBarsRequest
            client = StockHistoricalDataClient(self.settings.alpaca_key_id,
                                               self.settings.alpaca_secret_key)
            bars = client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=assets.normalise(symbol), timeframe=frame,
                start=start))

        now = datetime.now(timezone.utc)
        out = []
        for bar in bars.data.get(assets.normalise(symbol), []):
            if bar.timestamp + period > now:
                continue                      # still forming
            out.append(bar)
        return out[-limit:]

    def position_qty(self, symbol: str) -> Decimal:
        """How much of `symbol` is actually held.

        The SLASHLESS spelling, because /v2/positions wants it that way and the
        SDK will not do it for us — see the class docstring. Protection is sized
        from this and never from the fill: Alpaca charges the crypto fee in
        kind, so a filled 0.001 BTC leaves 0.0009975, and a stop sized to the
        fill asks to sell more than is held.

        Flat returns 0 rather than raising. Alpaca answers 404 for a symbol with
        no position, and that is an answer.
        """
        try:
            position = self.client.get_open_position(
                assets.normalise(symbol).replace("/", ""))
        except Exception as exc:
            if _is_not_found(exc):
                return Decimal("0")
            raise
        try:
            return Decimal(str(position.qty))
        except (AttributeError, TypeError, InvalidOperation) as exc:
            raise RuntimeError(
                f"Alpaca position response had no usable qty for {symbol}") from exc

    def _latest_crypto_price(self, symbol: str) -> float:
        from alpaca.data.historical import CryptoHistoricalDataClient
        from alpaca.data.requests import CryptoLatestTradeRequest

        client = CryptoHistoricalDataClient(self.settings.alpaca_key_id,
                                            self.settings.alpaca_secret_key)
        trades = client.get_crypto_latest_trade(
            CryptoLatestTradeRequest(symbol_or_symbols=assets.normalise(symbol)))
        trade = trades.get(assets.normalise(symbol))
        price = float(getattr(trade, "price", 0) or 0)
        if price <= 0:
            raise RuntimeError(
                f"Alpaca crypto market-data returned no trade price for {symbol}")
        return price

    def latest_trade_price(self, symbol: str) -> float:
        """Crypto lives on a different client with a different response shape.

        Asking the equity one for BTC/USD does not fail loudly; it returns
        nothing usable, which a notional check would read as a price of zero.
        """
        if is_crypto(symbol):
            return self._latest_crypto_price(symbol)

        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestTradeRequest

        client = StockHistoricalDataClient(self.settings.alpaca_key_id,
                                           self.settings.alpaca_secret_key)
        trades = client.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=assets.normalise(symbol)))
        trade = trades.get(assets.normalise(symbol))
        price = float(getattr(trade, "price", 0) or 0)
        if price <= 0:
            raise RuntimeError(
                f"Alpaca market-data returned no trade price for {symbol}")
        return price


def _parse_timeframe(timeframe: str):
    """TradingView's interval -> an Alpaca TimeFrame.

    TradingView writes minutes as a bare number and coarser frames with a
    suffix. An interval we cannot read raises rather than defaulting: a silent
    fallback to 1m would seed a 1h runner with the wrong bars, and every stop
    that followed would be several times tighter than the strategy was tested
    with, with nothing looking wrong.
    """
    from alpaca.data.timeframe import TimeFrameUnit

    text = (timeframe or "").strip()
    if text.isdigit():
        return int(text), TimeFrameUnit.Minute
    if len(text) > 1 and text[:-1].isdigit():
        suffix = text[-1].upper()
        unit = {"S": None, "M": TimeFrameUnit.Minute, "H": TimeFrameUnit.Hour,
                "D": TimeFrameUnit.Day}.get(suffix)
        if unit is not None:
            return int(text[:-1]), unit
    unit = {"D": TimeFrameUnit.Day, "W": TimeFrameUnit.Week,
            "M": TimeFrameUnit.Month}.get(text.upper())
    if unit is not None:
        return 1, unit
    raise ValueError(f"cannot interpret {timeframe!r} as a bar size")


def _is_not_found(exc: Exception) -> bool:
    """Alpaca's 404, however the SDK happens to surface it.

    Matched on the status code where there is one rather than on message text,
    because a substring check would also swallow a genuine failure that merely
    mentioned the number — and swallowing a failed lookup as "flat" is how a
    filled position loses its stop.
    """
    code = getattr(exc, "status_code", None)
    if code is not None:
        return int(code) == 404
    return "position does not exist" in str(exc).lower() or "not found" in str(exc).lower()


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

    def recent_bars(self, symbol: str, timeframe: str, limit: int = 30) -> list:
        return list(getattr(self, "bars", []))

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

    def recent_bars(self, symbol: str, timeframe: str, limit: int = 30) -> list:
        """Completed bars, newest last, for seeding a runner's trail at startup.

        Without this a gateway that restarts mid-position trails nothing until
        the first live bar arrives — up to a minute on 1m, and indefinitely if
        the single market-data connection slot is held by another process. The
        stop would sit at its last persisted value while price moved.

        The forming bar is excluded by its own timestamp rather than by
        dropping the newest row, because whether Alpaca includes it varies and
        a positional rule would silently discard a completed bar on the runs
        where it does not.
        """
        from datetime import datetime, timedelta, timezone

        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        amount, unit = _parse_timeframe(timeframe)
        period = timedelta(**{{"Min": "minutes", "Hour": "hours",
                               "Day": "days"}[unit.value]: amount})
        start = datetime.now(timezone.utc) - period * (limit + 2)
        frame = TimeFrame(amount, unit)

        if is_crypto(symbol):
            from alpaca.data.historical import CryptoHistoricalDataClient
            from alpaca.data.requests import CryptoBarsRequest
            client = CryptoHistoricalDataClient(self.settings.alpaca_key_id,
                                                self.settings.alpaca_secret_key)
            bars = client.get_crypto_bars(CryptoBarsRequest(
                symbol_or_symbols=assets.normalise(symbol), timeframe=frame,
                start=start))
        else:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockBarsRequest
            client = StockHistoricalDataClient(self.settings.alpaca_key_id,
                                               self.settings.alpaca_secret_key)
            bars = client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=assets.normalise(symbol), timeframe=frame,
                start=start))

        now = datetime.now(timezone.utc)
        out = []
        for bar in bars.data.get(assets.normalise(symbol), []):
            if bar.timestamp + period > now:
                continue                      # still forming
            out.append(bar)
        return out[-limit:]

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
