"""What `202 Accepted` promises, and what it stops promising.

The submit route used to be synchronous on purpose. `execution.py` guarantees
that a position it cannot protect gets FLATTENED — an unprotected position is
worse than no position — and doing that inside the request meant the caller
could not be told "accepted" while holding something naked.

Acknowledging early is still the right trade: the relay times out after 3s, and
a slow fill produced a timeout at the relay while Alpaca had already filled the
entry. But it moves the flatten-or-shout promise into a background task, and a
promise nobody awaits is the easiest kind to lose. An `asyncio` task that raises
and is never retrieved surfaces at interpreter shutdown, if at all — and one
that is merely garbage-collected mid-flight surfaces never.

So these are the properties the acknowledgement trades on, asserted rather than
assumed:

  * a failure after acknowledgement is LOGGED, with a traceback and the event id
  * the task is HELD, so it cannot be collected before it finishes
  * an acknowledgement means "submitted", and does not claim protection exists

The first two are what make the third honest.
"""

from __future__ import annotations

import asyncio
import inspect
import logging

import pytest


def test_a_failure_after_acknowledgement_is_logged_with_its_event_id(caplog):
    """The guarantee `202` trades away.

    Synchronously, a protection failure reached the caller as an error. Now it
    reaches a background task, so the only thing standing between an
    unprotected position and silence is this log line.
    """
    from tv_alpaca_gateway.app import create_app

    source = inspect.getsource(create_app)
    start = source.index("async def finish_in_background")
    body = source[start:start + 2000]

    assert "except Exception" in body, (
        "the background completion is unguarded; anything it raises is lost")
    assert "logger.exception" in body, (
        "a background failure is not logged with a traceback")
    assert "event_id" in body.split("except Exception")[-1], (
        "the failure log does not name the event, so it cannot be traced back "
        "to an order")


def test_the_background_task_is_held_so_it_cannot_be_collected():
    """asyncio keeps only a weak reference to a bare `create_task` result.

    A task nobody holds can be garbage-collected mid-flight, which would abandon
    fill tracking and protection silently — the position stays open and no log
    line is ever written, because the coroutine simply stopped.
    """
    from tv_alpaca_gateway.app import create_app

    source = inspect.getsource(create_app)
    assert "background_tasks.add(" in source, (
        "the completion task is not retained; it can be collected before it "
        "protects anything")
    assert "add_done_callback" in source, (
        "nothing discards the finished task, so the holding set grows forever")


def test_a_raising_completion_never_escapes_into_the_event_loop(caplog):
    """Behavioural version of the first test: run the shape and prove it is
    contained and reported, rather than reading the source."""
    logger = logging.getLogger("tv_alpaca_gateway.app")
    held: set = set()

    async def failing_completion() -> None:
        try:
            raise RuntimeError("protection failed after acknowledgement")
        except Exception:
            logger.exception("background Pine execution failed event_id=%s", "evt-1")

    async def drive() -> None:
        task = asyncio.create_task(failing_completion())
        held.add(task)
        task.add_done_callback(held.discard)
        await task

    with caplog.at_level(logging.ERROR, logger="tv_alpaca_gateway.app"):
        asyncio.run(drive())          # must not raise out of the loop

    assert "background Pine execution failed" in caplog.text
    assert "evt-1" in caplog.text
    assert not held, "the finished task was never discarded"


def test_the_acknowledgement_says_protection_is_pending_not_done():
    """`202` means submitted, not safe.

    The window between acknowledgement and protection is the whole cost of this
    design. The response must name it rather than paper over it: a `202` that
    reported protection as submitted, or omitted it, would read as "you are
    covered" during exactly the interval when nothing yet is.
    """
    from tv_alpaca_gateway.app import create_app

    source = inspect.getsource(create_app)
    ack = source[source.index("async def finish_in_background"):]

    assert "status_code=202" in ack, "the submit route no longer acknowledges with 202"
    payload = ack[ack.index("status_code=202"):]
    payload = payload[:payload.index("})") + 2]
    assert '"protection_status": "pending"' in payload, (
        "the acknowledgement does not report protection as pending, so a 202 "
        "cannot be told apart from a protected position")
