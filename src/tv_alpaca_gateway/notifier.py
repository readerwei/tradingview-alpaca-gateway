from __future__ import annotations

import logging

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
        # Never raises at the caller. This is called from inside the
        # trade-update message loop, where urllib's HTTPError — a subclass of
        # OSError — was being caught by the reconnect handler and reported as
        # "stream disconnected: HTTP Error 403: Forbidden". A healthy socket
        # was torn down on every order update because Discord was unhappy.
        #
        # Telling someone about a fill is not part of processing the fill.
        try:
            with urllib.request.urlopen(request, timeout=3):
                return
        except Exception as exc:
            logger.warning("notification failed (%s); continuing", exc)
            return
            pass


logger = logging.getLogger(__name__)


class NullNotifier:
    def send(self, content: str) -> None:
        return None
