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

from tv_alpaca_gateway.broker import AlpacaPaperClient, FakeBroker

# Every attribute the engine reaches for on a broker. Derived from reading
# execution.py; if the engine grows a call, this list must grow with it — and
# the test below is what makes forgetting expensive rather than silent.
ENGINE_REQUIRES = ("submit_order", "cancel_order", "get_order",
                   "latest_trade_price", "position_qty")


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
    from tv_alpaca_gateway import execution

    source = inspect.getsource(execution)
    called = {line.split("broker.")[1].split("(")[0]
              for line in source.splitlines() if "broker." in line
              and "(" in line.split("broker.")[-1]}
    called = {name for name in called if name.isidentifier()}

    unlisted = called - set(ENGINE_REQUIRES)
    assert not unlisted, (
        f"the engine calls {sorted(unlisted)} but this file does not check for "
        f"it; add it to ENGINE_REQUIRES")
