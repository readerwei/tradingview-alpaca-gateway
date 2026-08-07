"""Asset-class rules. Crypto is not "stocks that trade at night".

Four differences change the order path, and every one of them silently produced
a wrong or impossible order before:

**Quantity is fractional.** Alpaca's minimum BTC order is 0.000015548 (about
$1). With an integer quantity the smallest expressible order is 1 BTC — roughly
$65,000 — which then failed the notional cap. So crypto looked like it was
blocked by risk limits when it was really blocked by the type of a field, and
raising MAX_NOTIONAL would not have helped.

**Time-in-force differs.** Alpaca rejects ``day`` on crypto; it wants ``gtc``
or ``ioc``. A day order would simply have been refused by the broker.

**The symbol carries a slash.** ``BTC/USD`` is the canonical form and the one
the data API returns, but ``symbol.isalnum()`` rejects it outright. ``BTCUSD``
passed validation and Alpaca happens to resolve it, so the two forms have to be
treated as the same asset rather than as different symbols.

**The market never closes**, so extended-hours flags are meaningless.

Crypto is declared explicitly: write the slash form in ALLOWED_SYMBOLS. Nothing
is inferred from the shape of a ticker, because guessing that ``ETHUSD`` is
crypto while ``USDJPY`` is not would be a guess, and a wrong guess routes an
order to the wrong asset class.
"""

from __future__ import annotations

from decimal import Decimal


def normalise(symbol: str) -> str:
    return (symbol or "").strip().upper()


def is_crypto(symbol: str) -> bool:
    """Crypto pairs are written with a slash: BTC/USD."""
    return "/" in normalise(symbol)


def resolve(symbol: str, allowed: frozenset[str] | set[str]) -> str:
    """Map an alert's symbol onto its allowlisted form.

    An alert may say ``BTCUSD`` while the allowlist declares ``BTC/USD``; both
    name one asset and Alpaca accepts either. Returns the allowlisted spelling
    so everything downstream — the store, the notifier, the order — uses one
    consistent name. Unknown symbols come back unchanged, for the allowlist
    check to reject.
    """
    symbol = normalise(symbol)
    if symbol in allowed:
        return symbol
    for candidate in allowed:
        if is_crypto(candidate) and candidate.replace("/", "") == symbol:
            return candidate
    return symbol


def time_in_force(symbol: str) -> str:
    """Alpaca rejects `day` on crypto orders."""
    return "gtc" if is_crypto(symbol) else "day"


def format_qty(qty: Decimal) -> str:
    """Render a quantity for the Alpaca API.

    ``str(Decimal("0.000015548"))`` is fine, but a Decimal that has been through
    arithmetic can render as ``1.5548E-5``, which the API rejects. Fixed-point
    formatting with the exponent normalised away avoids that, and trailing
    zeros are trimmed so an equity order still reads "1" and not "1.000000".
    """
    text = format(qty.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
