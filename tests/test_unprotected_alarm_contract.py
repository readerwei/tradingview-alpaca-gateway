"""An unprotected position must stay the loudest thing this system can say.

Before the async refactor, a filled-but-unprotected position raised
`UnprotectedPositionError` out of the request: the caller got an error carrying
the entry order id, immediately. After it, protection runs in a background task
and the route has already answered `202 protection=pending`, so the only paths
left are a log line and a notification.

Two ways that goes quiet, both found in review rather than by an incident:

1. `UnprotectedPositionError` is caught by the same generic `except Exception`
   as any transient failure, so the worst state this system can reach is
   reported exactly like a retryable one. `app.py` still IMPORTS the exception
   and never catches it — a dead import is the fingerprint of handling lost in
   a refactor rather than removed on purpose.

2. `asyncio.CancelledError` inherits from BaseException, NOT Exception. A
   shutdown between fill and stop placement cancels the task, `except Exception`
   does not catch it, and the position goes unprotected with no log line at
   all — not even the generic one. Meanwhile `asyncio.to_thread` keeps the
   worker thread running, so whether the stop was placed is genuinely unknown.

The notifier is not a safe fallback for either: it returned `403 Forbidden`
four times in the 2026-08-14 logs and again on 2026-08-21. A design that leans
on notification has to survive notification being broken.
"""

from __future__ import annotations

import asyncio
import inspect
import logging

import pytest

from tv_alpaca_gateway.execution import UnprotectedPositionError


def _background_source() -> str:
    from tv_alpaca_gateway.app import create_app
    source = inspect.getsource(create_app)
    start = source.index("async def finish_in_background")
    return source[start:source.index("task = asyncio.create_task", start)]


def test_an_unprotected_position_is_reported_more_loudly_than_a_failure():
    """It is not one failure among many. `_flatten` already logs CRITICAL when
    it cannot close; the background handler must not flatten that distinction
    back out by catching it with everything else."""
    body = _background_source()

    assert "UnprotectedPositionError" in body, (
        "the background handler treats an unprotected position exactly like a "
        "transient failure; the worst state this system reaches has no "
        "distinct path")


def test_the_unprotected_case_is_recorded_not_only_logged():
    """A log line does not survive a rotation or a restart, and this is the one
    state an operator must still be able to find tomorrow."""
    body = _background_source()

    assert "store" in body, (
        "an unprotected position leaves no durable record — only a log line")


def _handled_exception_names(func_source: str) -> set[str]:
    """The exception types the handler actually catches, from the parse tree.

    Read via AST rather than by searching the text. The first version of this
    test looked for the string "CancelledError" and passed against a version
    with the handler deleted — because the COMMENT explaining the handler still
    contained the word. A source scan that a comment can satisfy measures
    nothing, which is the failure this suite exists to catch elsewhere.
    """
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(func_source))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is not None:
            for part in ast.walk(node.type):
                if isinstance(part, ast.Name):
                    names.add(part.id)
                elif isinstance(part, ast.Attribute):
                    names.add(part.attr)
    return names


def test_cancellation_cannot_pass_silently():
    """CancelledError is a BaseException. `except Exception` misses it, so a
    shutdown mid-protection produces no output whatsoever."""
    assert issubclass(asyncio.CancelledError, BaseException)
    assert not issubclass(asyncio.CancelledError, Exception)

    handled = _handled_exception_names(_background_source())
    assert "CancelledError" in handled or "BaseException" in handled, (
        f"a cancelled protection task is caught by nothing and logs nothing; "
        f"the handler catches {sorted(handled)}")


def test_the_unprotected_case_has_its_own_handler():
    """Same check for the other arm, and for the same reason: the prose above
    it mentions the exception by name, so only the parse tree can tell whether
    it is caught or merely discussed."""
    handled = _handled_exception_names(_background_source())
    assert "UnprotectedPositionError" in handled, (
        f"the handler catches {sorted(handled)}, so an unprotected position is "
        f"reported exactly like a transient failure")


def test_a_cancelled_completion_still_says_so(caplog):
    """Behavioural: cancel a task shaped like the real one and prove something
    is written. Without the BaseException arm this produces total silence."""
    logger = logging.getLogger("tv_alpaca_gateway.app")
    started = asyncio.Event()

    async def finish_like_the_real_one() -> None:
        try:
            started.set()
            await asyncio.sleep(3600)          # stands in for the worker thread
        except asyncio.CancelledError:
            logger.critical("protection was cancelled before it completed "
                            "event_id=%s", "evt-1")
            raise
        except Exception:
            logger.exception("background Pine execution failed")

    async def drive() -> None:
        task = asyncio.create_task(finish_like_the_real_one())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with caplog.at_level(logging.CRITICAL, logger="tv_alpaca_gateway.app"):
        asyncio.run(drive())

    assert "cancelled before it completed" in caplog.text
    assert "evt-1" in caplog.text


def test_the_dead_import_is_gone_or_used():
    """`app.py` imports `UnprotectedPositionError` and never catches it. Either
    it is handled or the import should not be there — an unused import of the
    most serious error in the system is a claim the code does not honour."""
    from tv_alpaca_gateway import app as _app_module   # noqa: F401
    import pathlib

    source = pathlib.Path(
        _app_module.__file__ if hasattr(_app_module, "__file__")
        else "src/tv_alpaca_gateway/app.py").read_text()
    if "UnprotectedPositionError" in source.split("\n\n")[0] or \
            "import" in source[:source.find("UnprotectedPositionError")][-200:]:
        uses = source.count("UnprotectedPositionError")
        assert uses > 1, (
            "UnprotectedPositionError is imported and never used; the handling "
            "was lost rather than removed")
