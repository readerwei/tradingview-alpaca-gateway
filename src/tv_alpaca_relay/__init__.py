"""Optional Discord-to-gateway relay component."""

from .relay import (
    ForwardResult,
    GatewayRelay,
    RelaySettings,
    admit_message,
    handle_message,
    run_relay,
)

__all__ = [
    "ForwardResult",
    "GatewayRelay",
    "RelaySettings",
    "admit_message",
    "handle_message",
    "run_relay",
]
