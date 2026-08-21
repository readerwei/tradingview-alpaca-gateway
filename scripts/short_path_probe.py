"""Exercise the SHORT path end to end against Alpaca paper.

Four sign bugs were found on 2026-08-21 — the entry gate, the flatten, the OCO
prices, and reconciliation — and every one of them was found by READING code
that reading had already approved. The short path had been written, reviewed,
merged, and never once run. That is the root cause; the arithmetic was only the
symptom.

This is the run that would have caught all four in a minute:

    1. short entry              does it fill?
    2. protection               does anything rest at the broker afterwards?
    3. reservation              what does a resting BUY stop do to a short's
                                qty_available? (never measured — the question
                                deferred since 2026-08-13)
    4. one reconcile interval   is the lot still open and protected, or does
                                reconciliation quietly close it?
    5. restart                  does the lot survive load_lot -> reconcile?

PLACES REAL PAPER ORDERS. Refuses unless the account is paper and `--place` is
given, and flattens whatever it opened before it exits — including on failure.

    set -a && . ~/.config/alpaca/paper.env && set +a
    uv run python scripts/short_path_probe.py            # dry run, no orders
    uv run python scripts/short_path_probe.py --place    # the measurement
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal

BASE = "https://paper-api.alpaca.markets/v2"


def _call(path: str, method: str = "GET", body: dict | None = None) -> object:
    key, secret = os.getenv("ALPACA_API_KEY_ID"), os.getenv("ALPACA_API_SECRET_KEY")
    if not key or not secret:
        raise SystemExit("ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY are not set")
    if not key.startswith("PK"):
        # The same rule as everywhere else in this project: a live key starts
        # AK, and this script places orders. Refuse on the key itself rather
        # than on a flag someone remembered to pass.
        raise SystemExit(f"refusing to run: key {key[:2]}… is not a paper key")
    request = urllib.request.Request(
        f"{BASE}{path}", method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read() or "null")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:200]
        raise SystemExit(f"{method} {path} -> {exc.code}: {detail}") from exc


def _position(symbol: str) -> dict | None:
    try:
        return _call(f"/positions/{symbol}")
    except SystemExit as exc:
        if "404" in str(exc):
            return None
        raise


def _step(n: int, what: str) -> None:
    print(f"\n[{n}] {what}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="QQQ")
    ap.add_argument("--qty", type=int, default=1,
                    help="keep it small; this is a measurement, not a trade")
    ap.add_argument("--reconcile-wait", type=float, default=65.0,
                    help="one reconcile interval plus a margin")
    ap.add_argument("--place", action="store_true")
    args = ap.parse_args()

    account = _call("/account")
    print(f"account   equity={float(account['equity']):,.2f}  "
          f"shorting_enabled={account['shortable_enabled' if 'shortable_enabled' in account else 'shorting_enabled']}")

    existing = _position(args.symbol)
    if existing:
        print(f"REFUSING: {args.symbol} already has a position "
              f"({existing['qty']}). This probe must start flat so the delta it "
              f"measures is its own.")
        return 2

    plan = [
        f"sell {args.qty} {args.symbol} market      -> expect a short fill",
        f"read position                             -> expect -{args.qty}",
        f"place buy stop above market               -> the protective order",
        f"read qty_available                        -> THE UNMEASURED QUESTION",
        f"wait {args.reconcile_wait:.0f}s                             -> one reconcile interval",
        f"read position and orders again            -> still short? still protected?",
        f"cancel + buy {args.qty} to flatten",
    ]
    print("\nplan:")
    for line in plan:
        print(f"  {line}")
    if not args.place:
        print("\ndry run — nothing submitted. Re-run with --place to measure.")
        return 0

    opened = False
    stop_id = None
    try:
        _step(1, "short entry")
        entry = _call("/orders", "POST", {
            "symbol": args.symbol, "qty": str(args.qty), "side": "sell",
            "type": "market", "time_in_force": "day",
            "client_order_id": f"short-probe-{int(time.time())}"})
        print(f"  submitted {entry['id']}")
        for _ in range(20):
            time.sleep(0.5)
            entry = _call(f"/orders/{entry['id']}")
            if entry["status"] in ("filled", "canceled", "rejected"):
                break
        print(f"  status={entry['status']} filled_qty={entry.get('filled_qty')} "
              f"avg={entry.get('filled_avg_price')}")
        if entry["status"] != "filled":
            print("  entry did not fill; nothing to measure")
            return 1
        opened = True

        _step(2, "position after the entry")
        position = _position(args.symbol)
        print(f"  qty={position['qty']}  side={position['side']}  "
              f"qty_available={position.get('qty_available')}")
        print(f"  -> the entry gate must read this as an ENTRY, not as "
              f"'nothing to protect'")

        _step(3, "protective buy stop above the market")
        price = Decimal(str(entry["filled_avg_price"]))
        stop_price = (price * Decimal("1.02")).quantize(Decimal("0.01"))
        stop = _call("/orders", "POST", {
            "symbol": args.symbol, "qty": str(args.qty), "side": "buy",
            "type": "stop", "stop_price": str(stop_price),
            "time_in_force": "gtc",
            "client_order_id": f"short-probe-stop-{int(time.time())}"})
        stop_id = stop["id"]
        print(f"  resting buy stop {stop_id} at {stop_price}")

        _step(4, "RESERVATION — the question deferred since 2026-08-13")
        time.sleep(2)
        position = _position(args.symbol)
        print(f"  qty={position['qty']}  qty_available={position.get('qty_available')}")
        print(f"  -> on a LONG, a resting sell stop reserves the whole position "
              f"and qty_available goes to 0.")
        print(f"  -> if a short behaves the same, the ladder's resize-before-sell "
              f"dance is needed on both sides. If not, the short path can be "
              f"simpler than the long one.")

        _step(5, f"waiting {args.reconcile_wait:.0f}s — one reconcile interval")
        time.sleep(args.reconcile_wait)
        position = _position(args.symbol)
        orders = _call(f"/orders?status=open&symbols={args.symbol}")
        print(f"  position: {position['qty'] if position else 'GONE'}")
        print(f"  open orders: {len(orders)}")
        print(f"  -> a gateway running this lot must still consider it open and "
              f"protected. #69 fixed reconciliation closing shorts; this is the "
              f"live confirmation.")
        return 0
    finally:
        if stop_id:
            print(f"\ncleanup: cancelling {stop_id}")
            try:
                _call(f"/orders/{stop_id}", "DELETE")
            except SystemExit as exc:
                print(f"  cancel failed: {exc}")
        if opened:
            print(f"cleanup: flattening {args.qty} {args.symbol}")
            try:
                _call("/orders", "POST", {
                    "symbol": args.symbol, "qty": str(args.qty), "side": "buy",
                    "type": "market", "time_in_force": "day",
                    "client_order_id": f"short-probe-flat-{int(time.time())}"})
                time.sleep(2)
                left = _position(args.symbol)
                print(f"  position now: {left['qty'] if left else 'flat'}")
            except SystemExit as exc:
                print(f"  FLATTEN FAILED: {exc}")
                print(f"  *** {args.symbol} MAY STILL BE SHORT — check the account ***")


if __name__ == "__main__":
    raise SystemExit(main())
