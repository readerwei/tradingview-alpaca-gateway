"""Check a real TradingView alert against the parser, field by field.

Usage:  uv run python check_alert.py '<the exact text TradingView sent>'

Reports what the parser makes of it, and — when it refuses — which field is
responsible, rather than just the first error.
"""

from __future__ import annotations

import sys

from tv_alpaca_gateway.pine_alert_parser import AlertParseError, parse_pine_alert

RAW = sys.argv[1] if len(sys.argv) > 1 else ""

print(f"raw ({len(RAW)} chars):")
print(f"  {RAW!r}\n")

if len(RAW) >= 2000:
    print("  ⚠ at or past Discord's 2000-char limit — this may have been split\n")

# What the alert actually contains, before the parser has an opinion.
parts = [p.strip() for p in RAW.split("|")]
print("fields as sent:")
for part in parts:
    if not part:
        continue
    if "=" in part:
        key, value = part.split("=", 1)
        print(f"  {key.strip():34s} = {value.strip()!r}")
    else:
        print(f"  {part.strip():34s}   (flag)")
print()

try:
    command = parse_pine_alert(RAW)
except AlertParseError as exc:
    print(f"PARSER REFUSED IT: {exc}\n")
    # Narrow it down: strip optional fields and see what still fails, so the
    # report names the responsible field rather than the first one hit.
    optional = ("CANCEL_UNFILLED_AT_DEADLINE", "STOP_TRIGGER", "STOP_LIMIT",
                "TRAIL", "PLACE_PROTECTIVE_STOP_AFTER_FILL", "REQUIRED_ACTIONS",
                "DO_NOT_SUMMARIZE_OR_REPOST_BEFORE_BROKER_CALL")
    for drop in optional:
        trimmed = " | ".join(p for p in parts if not p.upper().startswith(drop))
        try:
            parse_pine_alert(trimmed)
            print(f"  -> it parses once {drop} is removed, so that field is the cause")
            break
        except AlertParseError:
            continue
    else:
        print("  -> the failure is in a required field: "
              "SYMBOL, SIDE, QTY, ORDER_TYPE or TIME_IN_FORCE")
    sys.exit(1)

print("PARSED\n")
for name in ("symbol", "side", "qty", "order_type", "time_in_force",
             "cancel_unfilled_at_deadline", "place_protective_stop_after_fill",
             "stop_trigger", "stop_limit", "trail"):
    print(f"  {name:34s} {getattr(command, name)!r}")

print("\nsanity:")
if command.stop_trigger and command.stop_limit:
    ok = (command.stop_limit <= command.stop_trigger if command.side == "buy"
          else command.stop_limit >= command.stop_trigger)
    print(f"  protective stop direction        {'correct' if ok else 'WRONG SIDE'}")
if "/" in command.symbol:
    print(f"  crypto: TIF must be gtc/ioc      {command.time_in_force}")
    print(f"  crypto: TRAIL must be absent     {command.trail!r}")
print("  NOTE: parsing is not approval — allowlist, sizing, notional and the")
print("        price collar are not consulted here.")
