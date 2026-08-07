from __future__ import annotations

import json
import urllib.error
import urllib.request
from decimal import Decimal

from .config import Settings


class AlpacaPaperExitBroker:
    """Small paper-only adapter used by ExitManager."""

    def __init__(self, settings: Settings):
        settings.validate()
        if not settings.alpaca_key_id or not settings.alpaca_secret_key:
            raise RuntimeError("Alpaca paper credentials are not configured")
        self.settings = settings

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        request = urllib.request.Request(
            f"{self.settings.alpaca_base_url.rstrip('/')}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={
                "content-type": "application/json",
                "APCA-API-KEY-ID": self.settings.alpaca_key_id,
                "APCA-API-SECRET-KEY": self.settings.alpaca_secret_key,
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                body = response.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"Alpaca order-management request failed: HTTP {exc.code}: {detail}") from exc

    def submit_limit(self, symbol, side, qty, limit_price: Decimal, client_order_id: str) -> str:
        raw = self._request(
            "POST",
            "/v2/orders",
            {
                "symbol": symbol,
                "side": side,
                "qty": str(qty),
                "type": "limit",
                "time_in_force": "gtc",
                "limit_price": str(limit_price),
                "client_order_id": client_order_id,
            },
        )
        return raw["id"]

    def submit_trailing_stop(self, symbol, side, qty, trail_percent: Decimal, client_order_id: str) -> str:
        raw = self._request(
            "POST",
            "/v2/orders",
            {
                "symbol": symbol,
                "side": side,
                "qty": str(qty),
                "type": "trailing_stop",
                "time_in_force": "gtc",
                "trail_percent": str(trail_percent),
                "client_order_id": client_order_id,
            },
        )
        return raw["id"]

    def cancel(self, order_id: str) -> None:
        self._request("DELETE", f"/v2/orders/{order_id}")

    def replace_qty(self, order_id: str, qty: int) -> None:
        self._request("PATCH", f"/v2/orders/{order_id}", {"qty": str(qty)})
