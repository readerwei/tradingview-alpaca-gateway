"""Shutdown must not abandon protection, and must say so when it does.

The old drain cancelled first and gathered after:

    for task in tuple(background_tasks):
        task.cancel()
    await asyncio.gather(*background_tasks, return_exceptions=True)

Three things wrong with that, in increasing order of seriousness.

It cancels work that was about to succeed — a completion 200ms from placing a
stop is killed for no reason. It treats `gather` returning as proof the work
stopped, when the work is inside `asyncio.to_thread`: cancelling the awaiting
coroutine does not stop the thread, so the process exits while a stop may still
be in flight. And `CancelledError` is a BaseException, so before #71 none of it
was even logged.

So: stop accepting new work, WAIT a bounded grace period, and only then cancel
what is left — loudly, naming the events whose protection is unresolved.

WHY THE INTEGRATION TEST HERE IS DIFFERENT
------------------------------------------
#71's checks read the parse tree. That was the right call for "does this
handler exist", and it is not enough here, because the property under test is
about a real thread outliving a real cancellation. An earlier version of a
structural test in this suite passed against a build with the handler deleted —
the comment above it contained the word being searched for. Structure is
checked once; behaviour is checked by running it.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest


# ── the property, exercised through a real to_thread ────────────────────────

def test_cancelling_the_task_does_not_stop_the_thread():
    """The assumption the old drain rested on, shown to be false.

    This is not a test of our code — it is a test of the platform behaviour our
    code has to survive. If this ever starts failing, the drain can be
    simplified; until then it cannot.
    """
    started, finished = threading.Event(), threading.Event()

    def blocking_work() -> None:
        started.set()
        time.sleep(0.4)          # stands in for placing a protective order
        finished.set()           # reached even though the task was cancelled

    async def drive() -> None:
        task = asyncio.create_task(asyncio.to_thread(blocking_work))
        await asyncio.get_running_loop().run_in_executor(None, started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The coroutine is gone. The thread is not.
        assert not finished.is_set(), "the thread finished before we could look"
        await asyncio.get_running_loop().run_in_executor(None, finished.wait, 2)
        assert finished.is_set(), (
            "the worker thread did not run to completion after cancellation")

    asyncio.run(drive())


def test_a_bounded_drain_waits_for_work_that_is_nearly_done():
    """A completion 200ms from placing a stop must not be killed by a shutdown
    that could have waited. The old drain cancelled unconditionally."""
    placed = []

    async def completion() -> None:
        await asyncio.sleep(0.2)
        placed.append("stop")

    async def drive() -> None:
        tasks = {asyncio.create_task(completion())}
        done, pending = await asyncio.wait(tasks, timeout=2.0)
        assert not pending, "the drain gave up on work that was about to finish"

    asyncio.run(drive())
    assert placed == ["stop"], "protection was abandoned by the drain"


def test_work_that_outlives_the_grace_period_is_reported_not_silently_dropped():
    """The honest failure. Something has to give after the bound, and what
    gives must be named — these are the events whose protection is unknown."""
    reported: list[str] = []

    async def never_finishes() -> None:
        await asyncio.sleep(3600)

    async def drive() -> None:
        task = asyncio.create_task(never_finishes(), name="pine-finish-evt-1")
        done, pending = await asyncio.wait({task}, timeout=0.1)
        for stuck in pending:
            reported.append(stuck.get_name())
            stuck.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    asyncio.run(drive())
    assert reported == ["pine-finish-evt-1"], (
        "work abandoned at shutdown was not named")


# ── the drain as wired into the app ─────────────────────────────────────────

def test_the_drain_waits_before_it_cancels():
    """Order matters: the old code cancelled first, so the grace period could
    never help anything."""
    import inspect

    from tv_alpaca_gateway.app import create_app

    source = inspect.getsource(create_app)
    drain = source[source.index("finally:"):]
    drain = drain[:drain.index("if stream is not None")]

    assert "asyncio.wait" in drain, (
        "the drain does not wait for in-flight completions; it still cancels "
        "them outright")
    wait_at = drain.index("asyncio.wait")
    cancel_at = drain.find("cancel()", wait_at)
    assert cancel_at > wait_at, (
        "cancellation still happens before the grace period, so the wait "
        "cannot save anything")


def test_the_grace_period_is_configurable_and_bounded():
    from tv_alpaca_gateway.app import SHUTDOWN_DRAIN_SECONDS

    assert SHUTDOWN_DRAIN_SECONDS > 0, "a zero grace period is the old behaviour"
    assert SHUTDOWN_DRAIN_SECONDS <= 60, (
        "an unbounded drain turns a restart into a hang; protection that has "
        "not settled in a minute is not going to")


# ── the alarm's own health ──────────────────────────────────────────────────

def test_healthz_says_whether_the_alarm_itself_is_working(tmp_path):
    """Four 403s in one afternoon looked like noise. They were the notification
    channel being dead for the whole session, while UNPROTECTED POSITION was
    being routed through it. "Is my alarm working" must be a field, not an
    inference from log volume."""
    from fastapi.testclient import TestClient

    from tv_alpaca_gateway.app import create_app
    from tv_alpaca_gateway.broker import FakeBroker
    from tv_alpaca_gateway.config import Settings
    from tv_alpaca_gateway.store import EventStore

    settings = Settings(
        paper_trading=True, trading_enabled=True, webhook_secret="s",
        allowed_symbols=frozenset({"QQQ"}), max_qty=10, max_notional=10_000.0,
        db_path=tmp_path / "h.sqlite3")
    app = create_app(settings, FakeBroker(), EventStore(settings.db_path))

    with TestClient(app) as client:
        body = client.get("/healthz").json()

    for field in ("notifier_configured", "notifier_last_ok", "notifier_last_error"):
        assert field in body, f"/healthz does not report {field}"
    assert body["notifier_configured"] is False, (
        "no webhook was configured, and healthz claims otherwise")


def test_a_failing_notifier_is_visible_without_reading_logs():
    """A broken webhook is worse than an absent one: the system believes it has
    an alarm. The last error has to be retrievable."""
    import urllib.error
    import urllib.request

    from tv_alpaca_gateway.notifier import DiscordNotifier

    def _forbidden(*a, **k):
        raise urllib.error.HTTPError("http://x", 403, "Forbidden", {}, None)

    notifier = DiscordNotifier("https://discord.example/webhook")
    original = urllib.request.urlopen
    urllib.request.urlopen = _forbidden
    try:
        notifier.send("anything")
    finally:
        urllib.request.urlopen = original

    assert notifier.last_error is not None, (
        "the notifier failed and kept no record of it")
    assert "403" in str(notifier.last_error)
    assert notifier.last_ok is None
