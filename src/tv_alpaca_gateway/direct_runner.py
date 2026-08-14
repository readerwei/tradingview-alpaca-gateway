"""Run a parsed Pine command directly, without TradingView or webhook HTTP.

The runner deliberately reuses the gateway's parser, application lifespan,
broker, store, supervisor, and execution engine. It is a paper-test/operator
entry point, not a second execution implementation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from . import execution
from .app import create_app
from .config import Settings
from .logging_setup import configure
from .pine_alert_parser import PineOrderCommand, parse_pine_alert


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Parse and optionally execute one Pine alert directly."
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--alert-file", default="-",
        help="Alert text file, or - for stdin (default: -).",
    )
    source.add_argument("--alert", help="Alert text supplied directly.")
    parser.add_argument(
        "--execute", action="store_true",
        help="Actually submit through the paper execution engine. Without this, parse only.",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Exit after execution setup. By default stay alive for managed exits.",
    )
    parser.add_argument(
        "--delivery-id",
        help="Durable identity when the alert has no EVENT_ID.",
    )
    parser.add_argument("--deadline-seconds", type=float, default=60.0)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    return parser


def command_payload(command: PineOrderCommand) -> dict[str, object]:
    """Return the parsed command without environment or credential data."""
    return {
        "symbol": command.symbol,
        "side": command.side,
        "qty": str(command.qty),
        "order_type": command.order_type,
        "time_in_force": command.time_in_force,
        "cancel_unfilled_at_deadline": command.cancel_unfilled_at_deadline,
        "place_protective_stop_after_fill": command.place_protective_stop_after_fill,
        "stop_trigger": str(command.stop_trigger) if command.stop_trigger is not None else None,
        "stop_limit": str(command.stop_limit) if command.stop_limit is not None else None,
        "take_profit": str(command.take_profit) if command.take_profit is not None else None,
        "trail": str(command.trail) if command.trail is not None else None,
        "exit_plan": command.exit_plan,
        "interval": command.interval,
    }


def _read_alert(args: argparse.Namespace) -> str:
    if args.alert is not None:
        return args.alert
    if args.alert_file == "-":
        return sys.stdin.read()
    return Path(args.alert_file).read_text(encoding="utf-8")


def _result_payload(result: execution.ExecutionResult) -> dict[str, object]:
    return {
        "entry_order_id": result.entry_order_id,
        "entry_status": result.entry_status,
        "protection_order_id": result.protection_order_id,
        "protection_status": result.protection_status,
    }


async def run(args: argparse.Namespace) -> int:
    try:
        command = parse_pine_alert(_read_alert(args))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2

    parsed = {"ok": True, "executed": False, "command": command_payload(command)}
    if not args.execute:
        print(json.dumps(parsed, sort_keys=True))
        return 0

    # --once and an EXIT_PLAN are a contradiction, and an expensive one.
    #
    # --once returns as soon as the entry is done, which closes the lifespan
    # and takes the supervisor, the market-data sockets and the reconcile timer
    # with it. A managed plan needs all three to stay alive: the entry fills,
    # the disaster stop is armed, the lot is written to the store — and then
    # nothing is left listening, so no rung ever fires.
    #
    # That combination cost two days. The ladder armed correctly four times and
    # never once ran, and every symptom pointed at the ladder: a take-profit
    # breached at 06:00 with no order, a stop cancelled mid-resize and never
    # replaced, /healthz refusing connections, the market-data slot flickering
    # between held and free. All of it was this flag.
    #
    # Refused rather than silently downgraded to a plain stop: someone asking
    # for a managed exit should be told they cannot have one here, not handed
    # something quieter that looks similar.
    if args.once and command.exit_plan:
        print(json.dumps({
            "ok": False,
            "error": (f"--once cannot run EXIT_PLAN={command.exit_plan}: it exits "
                      f"as soon as the entry fills, which stops the supervisor "
                      f"that manages the exits. Drop --once to stay alive, or "
                      f"run the gateway under uvicorn and submit through the relay."),
        }), file=sys.stderr)
        return 2

    settings = Settings.from_env()
    configure(settings)
    if not settings.paper_trading:
        print(json.dumps({"ok": False, "error": "direct runner requires PAPER_TRADING=true"}),
              file=sys.stderr)
        return 2

    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        result = await asyncio.to_thread(
            execution.execute_pine_command,
            command,
            settings,
            app.state.broker,
            app.state.store,
            deadline_seconds=args.deadline_seconds,
            poll_interval=args.poll_interval,
            delivery_id=args.delivery_id,
            supervisor=app.state.supervisor,
        )
        output = {
            "ok": True,
            "executed": True,
            "command": command_payload(command),
            "result": _result_payload(result),
        }
        print(json.dumps(output, sort_keys=True))
        if args.once or result.entry_status in {"kill_switch", "duplicate", "rejected"}:
            return 0
        print("direct runner active; managed exits remain running; press Ctrl-C to stop",
              file=sys.stderr)
        await asyncio.Event().wait()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
