"""A notification failing must not be a trading failure.

Observed live on 2026-08-14. Every order update was followed ~150ms later by:

    WARNING Alpaca trade_updates stream disconnected: HTTP Error 403: Forbidden

Four for four, correlated with order activity. It looked like another consumer
evicting our connection — Wei pushed back that nothing else was running, and
the chain turned out to be entirely ours:

    order update -> on_order_update -> notifier.send -> Discord 403
                 -> urllib.error.HTTPError, a subclass of OSError
                 -> caught by the reconnect loop as a stream failure
                 -> healthy socket torn down, reconnect, repeat

The error text was the clue: `HTTP Error 403: Forbidden` is a urllib error. A
websocket eviction raises ConnectionClosed with a close code and looks nothing
like it.

Worse than the churn: `notifier.send` sat BEFORE the line that routes the fill
to the lot, so the exception skipped it. The primary fill path was dead for as
long as the webhook was broken, disguised as a network problem, and only the
60-second reconcile timer kept the ladder correct.
"""

from __future__ import annotations

import logging

import pytest


def test_the_notifier_never_raises_at_its_caller(caplog):
    """It is called from inside a message loop. Anything it raises becomes
    that loop's problem, and urllib's errors are OSErrors."""
    import urllib.error
    import urllib.request

    from tv_alpaca_gateway.notifier import DiscordNotifier

    def _forbidden(*a, **k):
        raise urllib.error.HTTPError("http://x", 403, "Forbidden", {}, None)

    notifier = DiscordNotifier("https://discord.example/webhook")
    original = urllib.request.urlopen
    urllib.request.urlopen = _forbidden
    try:
        with caplog.at_level(logging.WARNING, logger="tv_alpaca_gateway"):
            notifier.send("anything")          # must not raise
    finally:
        urllib.request.urlopen = original

    assert "notification failed" in caplog.text, "the failure was swallowed silently"


def test_a_failing_notifier_does_not_stop_the_fill_reaching_the_lot():
    """The consequence that mattered. Notifying used to come first, so the 403
    raised past the routing call and the lot never learned about its own fill."""
    import inspect

    # Import the FUNCTION, not the module. `tv_alpaca_gateway.app` resolves to
    # the FastAPI instance, because app.py defines `app = create_app()` at
    # module level and that name shadows the module on the package. This has
    # now caught three separate tests.
    from tv_alpaca_gateway.app import create_app

    source = inspect.getsource(create_app)
    routing = source.index("supervisor.on_order_update")
    notifying = source.index("Alpaca paper order update")

    assert routing < notifying, (
        "the notification is sent before the fill is routed; if it raises, the "
        "lot never hears about the fill")


def test_an_order_update_handler_failure_is_not_a_disconnect():
    """A handler bug must not masquerade as a network problem. It did for a
    day, and sent us looking at Alpaca's connection limits."""
    import inspect

    from tv_alpaca_gateway import stream

    source = inspect.getsource(stream.AlpacaTradeUpdateStream._run_once)
    assert "except Exception" in source, (
        "the update handler is unguarded, so anything it raises is reported as "
        "a stream disconnect")
