"""The real broker must implement what the engine calls on it.

WHY THIS FILE EXISTS
--------------------
The Stage 3 contract used fake brokers, and its docstring said outright that
they "double as the specification of the broker interface the executor may rely
on". That made the fakes normative — and nothing ever checked that
`AlpacaPaperClient` matched them.

It didn't. The engine calls `broker.submit_order(**kwargs)`; the real client
only had `submit(ApprovedOrder, client_order_id)`. Every one of the 168 tests
passed, and the first genuine paper order returned:

    AttributeError: 'AlpacaPaperClient' object has no attribute 'submit_order'
    502 Bad Gateway

25 contract cases exercised the execution logic in detail and not one
instantiated the class that would run in production. The fakes agreed with the
engine, the engine agreed with the fakes, and the real client was never part of
the conversation.

These checks need no credentials and no network: they compare call signatures,
which is exactly the class of mismatch that survived everything else.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

from tv_alpaca_gateway.broker import AlpacaPaperClient, FakeBroker

# Every attribute the engine reaches for on a broker. Derived from reading
# execution.py; if the engine grows a call, this list must grow with it — and
# the test below is what makes forgetting expensive rather than silent.
ENGINE_REQUIRES = ("submit_order", "cancel_order", "get_order",
                   "latest_trade_price", "position_qty",
                   # Added by the exit manager and its supervisor. The
                   # guard below scans those modules too — without that it
                   # kept passing while this exact gap reopened, and the
                   # first ladder would have failed the way the first order
                   # did.
                   "get_order_by_client_id", "open_orders",
                   "min_order_size", "fill_price", "recent_bars")


def _missing(cls) -> list[str]:
    return [name for name in ENGINE_REQUIRES if not callable(getattr(cls, name, None))]


def test_the_real_client_implements_everything_the_engine_calls():
    """The production client, not a fake.

    This is the assertion whose absence let a 502 reach the first real order.
    """
    missing = _missing(AlpacaPaperClient)
    assert not missing, (
        f"AlpacaPaperClient is missing {missing}, which the execution engine "
        f"calls; every order through the submit route would fail")


def test_the_fake_broker_implements_it_too():
    """Otherwise the fakes drift from the real client in the other direction,
    and tests start passing against an interface nothing implements."""
    assert not _missing(FakeBroker)


def test_submit_order_accepts_the_arguments_the_engine_sends():
    """Presence is not conformance. The engine calls submit_order with keyword
    arguments only, so a positional-only signature would satisfy the check
    above and still fail at runtime.
    """
    signature = inspect.signature(AlpacaPaperClient.submit_order)
    kinds = {p.kind for name, p in signature.parameters.items() if name != "self"}
    assert inspect.Parameter.VAR_KEYWORD in kinds or all(
        k in {inspect.Parameter.KEYWORD_ONLY,
              inspect.Parameter.POSITIONAL_OR_KEYWORD} for k in kinds), (
        "submit_order cannot be called with the keyword arguments the engine sends")


def test_the_engine_requirement_list_matches_what_the_engine_actually_calls():
    """Guards the guard.

    ENGINE_REQUIRES is hand-written, so it can fall behind the engine and this
    file would keep passing while the real gap reopened. Reading the engine's
    own source keeps the list honest.
    """
    from tv_alpaca_gateway import execution, exit_manager, lot_supervisor

    called = set()
    for module in (execution, exit_manager, lot_supervisor):
        source = inspect.getsource(module)
        for line in source.splitlines():
            for marker in ("broker.", "_broker."):
                if marker in line and "(" in line.split(marker)[-1]:
                    name = line.split(marker)[1].split("(")[0]
                    if name.isidentifier():
                        called.add(name)

    unlisted = called - set(ENGINE_REQUIRES)
    assert not unlisted, (
        f"the engine calls {sorted(unlisted)} but this file does not check for "
        f"it; add it to ENGINE_REQUIRES")


class _SpyClient:
    """Stands in for alpaca-py's TradingClient and records what it was handed.

    The old versions of the three tests below asserted on a urllib URL. The
    property they protect is unchanged — only the boundary moved, from the URL
    we built to the argument the SDK is given. That boundary matters MORE now,
    because the SDK interpolates the symbol into a path without encoding it.
    """

    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    def get_open_position(self, symbol):
        self.calls.append(("get_open_position", symbol))
        raise _NotFound()

    def get_orders(self, request):
        self.calls.append(("get_orders", request))
        return []

    def get_asset(self, symbol):
        self.calls.append(("get_asset", symbol))
        return type("A", (), {"min_order_size": "0.000015437"})()


class _NotFound(Exception):
    status_code = 404


def _spied() -> tuple[AlpacaPaperClient, _SpyClient]:
    from tv_alpaca_gateway.config import Settings

    client = AlpacaPaperClient(Settings(alpaca_key_id="k", alpaca_secret_key="s"))
    spy = _SpyClient()
    client._client = spy
    return client, spy


def test_position_lookup_drops_the_slash_the_sdk_would_have_kept():
    """The SDK will not do this for us, and the failure is silent.

    From alpaca-py's own source:

        symbol_or_asset_id = validate_symbol_or_asset_id(symbol_or_asset_id)
        response = self.get(f"/positions/{symbol_or_asset_id}")

    That validator checks the TYPE and returns the string untouched, and
    nothing in the client URL-encodes. So "BTC/USD" becomes /positions/BTC/USD,
    a different route — and a 404 legitimately means flat, so a held position
    would read as zero, the engine would conclude there is nothing to protect,
    and a filled position would keep no stop.
    """
    client, spy = _spied()

    client.position_qty("BTC/USD")

    assert spy.calls == [("get_open_position", "BTCUSD")], (
        f"position lookup passed {spy.calls}; a slash reaches a different "
        f"route, and its 404 reads as flat")


def test_open_orders_keeps_the_slash_that_positions_drops():
    """The two endpoints want opposite spellings and both fail quietly.

    Measured against the live paper account with one resting stop on BTC/USD:

        symbols=BTC%2FUSD  -> 1 order
        symbols=BTCUSD     -> 0 orders, HTTP 200

    The wrong spelling reports no open orders rather than erroring, and "no
    open orders" is what reconciliation reads as "the stop is gone" — so it
    cancels nothing and places a duplicate.
    """
    client, spy = _spied()

    client.open_orders("BTC/USD")

    name, request = spy.calls[0]
    assert name == "get_orders"
    assert list(request.symbols) == ["BTC/USD"], (
        f"open_orders asked for {request.symbols}; the slashless form returns "
        f"an empty list with HTTP 200")


def test_min_order_size_is_asked_for_rather_than_hardcoded():
    """It moves. Alpaca recalculates the crypto minimum against price, and it
    changed between two checks hours apart on the same night:

        0.000015417  ->  0.000015437

    A constant baked in at development time is therefore wrong by an unknown
    amount whenever it matters.
    """
    client, spy = _spied()

    assert client.min_order_size("BTC/USD") == Decimal("0.000015437")
    assert ("get_asset", "BTC/USD") in spy.calls
    assert client.min_order_size("QQQ") == Decimal("1"), "equities are whole shares"


def test_a_failed_lookup_is_not_mistaken_for_a_flat_position():
    """404 means flat; anything else means we do not know.

    Swallowing a failed lookup as zero is how a filled position loses its stop
    — the engine concludes there is nothing to protect and returns cleanly.
    """
    client, spy = _spied()

    class _Boom(Exception):
        status_code = 500

    spy.get_open_position = lambda symbol: (_ for _ in ()).throw(_Boom())
    with pytest.raises(Exception):
        client.position_qty("BTC/USD")


def test_sdk_enums_are_unwrapped_to_the_strings_the_engine_compares_against():
    """Found against the live account, not by a test — the spy was too polite.

    alpaca-py returns `OrderSide.SELL` and `OrderType.STOP_LIMIT`. `str()` on
    those yields "OrderSide.SELL", which never equals "sell" — so
    `open_lot` stops seeing a resting stop as a resting sell, the one-lot
    conflict check passes when it should refuse, and reconciliation concludes
    the protection is gone and places a duplicate. Nothing raises.
    """
    import enum

    class _Side(enum.Enum):
        SELL = "sell"

    class _Type(enum.Enum):
        STOP_LIMIT = "stop_limit"

    class _Order:
        def model_dump(self):
            return {"id": "o1", "side": _Side.SELL, "order_type": _Type.STOP_LIMIT,
                    "type": _Type.STOP_LIMIT, "status": "new", "filled_qty": "0",
                    "qty": "0.00149625"}

    out = AlpacaPaperClient._as_dict(_Order())

    assert out["side"] == "sell", f"side came back as {out['side']!r}"
    assert out["type"] == "stop_limit", f"type came back as {out['type']!r}"
