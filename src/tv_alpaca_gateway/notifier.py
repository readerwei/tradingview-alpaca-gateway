from __future__ import annotations

import json
import urllib.request


class DiscordNotifier:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send(self, content: str) -> None:
        if not self.webhook_url:
            return
        request = urllib.request.Request(
            self.webhook_url,
            data=json.dumps({"content": content}).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3):
            pass


class NullNotifier:
    def send(self, content: str) -> None:
        return None
