"""Optional Discord-to-gateway relay component."""

from .relay import (
    GatewayRelay,
    RelaySettings,
    admit_message,
    handle_message,
    run_relay,
)

__all__ = [
    "GatewayRelay",
    "RelaySettings",
    "admit_message",
    "handle_message",
    "run_relay",
]
