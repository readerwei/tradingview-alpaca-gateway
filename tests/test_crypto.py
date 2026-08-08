"""Crypto support: fractional sizing, symbol forms, TIF, and its own stream."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tv_alpaca_gateway import assets
from tv_alpaca_gateway.config import Settings
from tv_alpaca_gateway.models import AlertError, Signal
from tv_alpaca_gateway.risk import RiskError, approve
from tv_alpaca_gateway.stream import AlpacaStreamManager

BTC = "BTC/USD"


def _settings(**kw):
    base = dict(allowed_symbols=frozenset({BTC, "QQQ"}),
                crypto_max_qty=Decimal("0.001"), max_qty=1,
                max_notional=1_000_000.0, max_price_deviation=0.05)
    base.update(kw)
    return Settings(**base)


def _alert(symbol=BTC, close=64_900.0, action="buy"):
    return Signal.parse({
        "event_id": "e1", "symbol": symbol, "action": action, "timeframe": "1",
        "close": close, "bar_time": datetime.now(timezone.utc).isoformat(),
    })


# ─────────────────────────────────────────────────────── the blocking bug

def test_a_crypto_order_can_be_smaller_than_one_coin():
    """With an integer qty the smallest possible BTC order was 1 coin — about
    $65,000 — against an Alpaca minimum of 0.000015548. Crypto was untradeable,
    and the failure surfaced as a notional-cap rejection, so raising the cap
    looked like the fix and could never be."""
    order = approve(_alert(), _settings(), reference_price=64_900.0)
    assert order.qty == Decimal("0.001")
    assert order.notional < Decimal("100")


def test_an_unsized_crypto_alert_names_the_real_cause():
    """Rejecting for "notional exceeds limit" when CRYPTO_MAX_QTY is unset sent
    an operator to raise MAX_NOTIONAL, which cannot help."""
    with pytest.raises(RiskError, match="CRYPTO_MAX_QTY"):
        approve(_alert(), _settings(crypto_max_qty=Decimal("0")),
                reference_price=64_900.0)


def test_config_refuses_a_crypto_allowlist_with_no_size():
    """Fail at startup rather than at the first alert."""
    with pytest.raises(ValueError, match="CRYPTO_MAX_QTY"):
        Settings(allowed_symbols=frozenset({BTC}),
                 crypto_max_qty=Decimal("0")).validate()


# ────────────────────────────────────────────────────────── symbol forms

def test_both_spellings_name_one_asset():
    """The alert may say BTCUSD; the canonical form Alpaca returns is BTC/USD.
    Both must resolve to the allowlisted spelling."""
    assert approve(_alert("BTCUSD"), _settings(), reference_price=64_900.0).symbol == BTC
    assert approve(_alert(BTC), _settings(), reference_price=64_900.0).symbol == BTC


def test_the_canonical_slash_form_parses():
    """`symbol.isalnum()` rejected BTC/USD — the only form that worked was the
    one Alpaca does not use itself."""
    assert Signal.parse({
        "event_id": "e", "symbol": "btc/usd", "action": "buy", "timeframe": "1",
        "close": 1.0, "bar_time": datetime.now(timezone.utc).isoformat(),
    }).symbol == BTC


@pytest.mark.parametrize("bad", ["BTC/", "/USD", "BTC//USD", "BTC/US/D", "BT C"])
def test_malformed_symbols_are_still_refused(bad):
    with pytest.raises(AlertError):
        Signal.parse({"event_id": "e", "symbol": bad, "action": "buy",
                      "timeframe": "1", "close": 1.0,
                      "bar_time": datetime.now(timezone.utc).isoformat()})


def test_an_unlisted_crypto_pair_is_refused():
    with pytest.raises(RiskError, match="allowlist"):
        approve(_alert("ETH/USD"), _settings(), reference_price=3000.0)


# ─────────────────────────────────────────────────────── order semantics

def test_crypto_uses_gtc_because_alpaca_rejects_day():
    assert approve(_alert(), _settings(), reference_price=64_900.0).time_in_force == "gtc"
    assert approve(_alert("QQQ", close=700.0), _settings(),
                   reference_price=700.0).time_in_force == "day"


def test_equities_are_unchanged():
    order = approve(_alert("QQQ", close=700.0), _settings(max_qty=3),
                    reference_price=700.0)
    assert order.qty == Decimal(3) and order.time_in_force == "day"
    assert not order.is_crypto


def test_the_notional_cap_still_binds_on_crypto():
    with pytest.raises(RiskError, match="notional"):
        approve(_alert(), _settings(crypto_max_qty=Decimal("1"), max_notional=1_000.0),
                reference_price=64_900.0)


def test_quantity_never_reaches_alpaca_in_scientific_notation():
    """format(Decimal("0.000015548")) can render as 1.5548E-5, which the API
    rejects. Trailing zeros are trimmed too, so an equity order stays "1"."""
    assert assets.format_qty(Decimal("0.000015548")) == "0.000015548"
    assert assets.format_qty(Decimal("1E-5")) == "0.00001"
    assert assets.format_qty(Decimal("1.000")) == "1"
    assert assets.format_qty(Decimal(3)) == "3"


# ───────────────────────────────────────────────────────────── streaming

def test_crypto_streams_from_its_own_endpoint():
    s = _settings(crypto_symbols=(BTC,), stream_enabled=True,
                  alpaca_key_id="k", alpaca_secret_key="s")
    manager = AlpacaStreamManager(s)
    assert manager.crypto is not None
    assert manager.crypto.url == "wss://stream.data.alpaca.markets/v1beta3/crypto/us"
    assert manager.crypto.symbols == [BTC]
    # the equity socket must not try to carry crypto
    assert manager.market.url != manager.crypto.url


def test_no_crypto_stream_without_crypto_symbols():
    """An empty subscribe list would hold a socket open receiving nothing."""
    assert AlpacaStreamManager(_settings(stream_enabled=True)).crypto is None


def test_crypto_symbols_must_use_the_slash_form():
    with pytest.raises(ValueError, match="slash"):
        Settings(allowed_symbols=frozenset({BTC}), crypto_max_qty=Decimal("0.001"),
                 crypto_symbols=("BTCUSD",)).validate()


# ────────────────────────────── mixed MARKET_SYMBOLS routing (found in review)

def test_mixed_market_symbols_are_split_across_two_sockets():
    """MARKET_SYMBOLS=BTC/USD,QQQ sent both to the equity endpoint.

    Alpaca answers {"T":"error","code":400,"msg":"invalid syntax"} — and that
    rejection kills the WHOLE subscription, so a single crypto symbol silently
    took the equity feed down with it. Reproduced live before fixing.

    The slash makes the routing unambiguous, so the symbols are partitioned
    rather than refused.
    """
    s = _settings(market_symbols=("BTC/USD", "QQQ"), stream_enabled=True,
                  alpaca_key_id="k", alpaca_secret_key="s")
    manager = AlpacaStreamManager(s)

    assert manager.market is not None and manager.market.symbols == ["QQQ"]
    assert manager.crypto is not None and manager.crypto.symbols == ["BTC/USD"]
    assert manager.market.url.endswith("/v2/iex")
    assert manager.crypto.url.endswith("/v1beta3/crypto/us")


def test_crypto_only_configuration_starts_no_equity_socket():
    """An empty subscribe list would hold a socket open receiving nothing."""
    s = _settings(market_symbols=("BTC/USD",), stream_enabled=True,
                  alpaca_key_id="k", alpaca_secret_key="s")
    manager = AlpacaStreamManager(s)
    assert manager.market is None
    assert manager.crypto.symbols == ["BTC/USD"]


def test_both_symbol_variables_feed_the_crypto_socket():
    """CRYPTO_SYMBOLS still works, and duplicates across the two lists collapse."""
    s = _settings(market_symbols=("QQQ", "BTC/USD"), crypto_symbols=("BTC/USD", "ETH/USD"),
                  stream_enabled=True, alpaca_key_id="k", alpaca_secret_key="s")
    assert AlpacaStreamManager(s).crypto.symbols == ["BTC/USD", "ETH/USD"]


def test_every_configured_symbol_reaches_exactly_one_socket():
    """Nothing dropped, nothing duplicated — the property that actually matters."""
    s = _settings(market_symbols=("QQQ", "BTC/USD", "AAPL", "ETH/USD"),
                  stream_enabled=True, alpaca_key_id="k", alpaca_secret_key="s")
    manager = AlpacaStreamManager(s)
    routed = manager.market.symbols + manager.crypto.symbols
    assert sorted(routed) == sorted(s.market_symbols)
    assert len(routed) == len(set(routed))
