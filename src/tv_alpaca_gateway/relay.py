from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


PINE_EXECUTION_PREFIX = "EXECUTE_ALPACA_ORDER"
DISCORD_MESSAGE_MAX_CHARS = 2000
PINE_DRY_RUN_PATH = "/webhooks/tradingview/pine/dry-run"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RelaySettings:
    token: str
    channel_id: int
    source_webhook_id: int
    internal_url: str = "http://127.0.0.1:8000/webhooks/tradingview"
    internal_secret: str = ""

    def validate_target(self) -> None:
        """Ensure this private relay can only reach the Pine dry-run route."""
        parsed = urlsplit(self.internal_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.path != PINE_DRY_RUN_PATH
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("relay target must be a local Pine dry-run endpoint")

    @classmethod
    def from_env(cls) -> "RelaySettings":
        token = os.getenv("DISCORD_BOT_TOKEN", "")
        channel_id = int(os.getenv("DISCORD_SIGNAL_CHANNEL_ID", "0"))
        source_webhook_id = int(os.getenv("DISCORD_SOURCE_WEBHOOK_ID", "0"))
        if not token or not channel_id or not source_webhook_id:
            raise ValueError("DISCORD_BOT_TOKEN, DISCORD_SIGNAL_CHANNEL_ID, and DISCORD_SOURCE_WEBHOOK_ID are required")
        return cls(
            token=token,
            channel_id=channel_id,
            source_webhook_id=source_webhook_id,
            internal_url=os.getenv("GATEWAY_INTERNAL_URL", cls.internal_url),
            internal_secret=os.getenv("TV_WEBHOOK_SECRET", ""),
        )


def admit_message(message: Any, settings: RelaySettings) -> str:
    """Return raw Pine text only from the configured channel and webhook."""
    if getattr(message.channel, "id", None) != settings.channel_id:
        raise ValueError("message is from an unapproved channel")
    if getattr(message, "webhook_id", None) != settings.source_webhook_id:
        raise ValueError("message is not from the approved source webhook")
    content = getattr(message, "content", None)
    if not isinstance(content, str) or not content:
        raise ValueError("source message has no Pine order content")
    if len(content) >= DISCORD_MESSAGE_MAX_CHARS:
        raise ValueError("Pine order exceeds Discord's 2000-character message limit")
    if not content.startswith(PINE_EXECUTION_PREFIX):
        raise ValueError("source message is not an EXECUTE_ALPACA_ORDER command")
    return content


class GatewayRelay:
    def __init__(self, settings: RelaySettings):
        settings.validate_target()
        self.settings = settings

    def forward(self, pine_alert: str) -> None:
        request = urllib.request.Request(
            self.settings.internal_url,
            data=pine_alert.encode("utf-8"),
            headers={"content-type": "text/plain; charset=utf-8", "x-tv-secret": self.settings.internal_secret},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3):
            pass


def handle_message(message: Any, settings: RelaySettings, relay: GatewayRelay) -> bool:
    """Admit and forward one Discord message.

    Returns True only when the raw Pine text was forwarded. Rejections are
    visible at WARNING level; forwarding failures remain visible as errors and
    are not retried.
    """
    try:
        payload = admit_message(message, settings)
        relay.forward(payload)
        return True
    except ValueError as exc:
        logger.warning("relay ignored a message: %s", exc)
        return False
    except Exception as exc:
        print(f"relay forwarding failed: {exc}")
        return False


def run_relay() -> None:
    logging.basicConfig(level=logging.INFO)
    try:
        import discord
    except ImportError as exc:
        raise RuntimeError("Install the relay extra with: uv sync --extra relay") from exc

    settings = RelaySettings.from_env()
    relay = GatewayRelay(settings)
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        print(f"Discord relay connected as {client.user}; listening on channel {settings.channel_id}")

    @client.event
    async def on_message(message):
        handle_message(message, settings, relay)

    client.run(settings.token)
