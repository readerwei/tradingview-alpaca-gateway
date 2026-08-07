from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RelaySettings:
    token: str
    channel_id: int
    source_webhook_id: int
    internal_url: str = "http://127.0.0.1:8000/webhooks/tradingview"
    internal_secret: str = ""

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


def admit_message(message: Any, settings: RelaySettings) -> dict:
    """Return a strict JSON signal only for the configured channel/webhook."""
    if getattr(message.channel, "id", None) != settings.channel_id:
        raise ValueError("message is from an unapproved channel")
    if getattr(message, "webhook_id", None) != settings.source_webhook_id:
        raise ValueError("message is not from the approved source webhook")
    if not isinstance(getattr(message, "content", None), str) or not message.content.strip():
        raise ValueError("source message has no JSON content")
    try:
        payload = json.loads(message.content)
    except json.JSONDecodeError as exc:
        raise ValueError("source message is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("source payload must be a JSON object")
    return payload


class GatewayRelay:
    def __init__(self, settings: RelaySettings):
        self.settings = settings

    def forward(self, payload: dict) -> None:
        request = urllib.request.Request(
            self.settings.internal_url,
            data=json.dumps(payload).encode(),
            headers={"content-type": "application/json", "x-tv-secret": self.settings.internal_secret},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3):
            pass


def run_relay() -> None:
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
        try:
            payload = admit_message(message, settings)
            relay.forward(payload)
        except ValueError:
            return
        except Exception as exc:
            print(f"relay forwarding failed: {exc}")

    client.run(settings.token)
