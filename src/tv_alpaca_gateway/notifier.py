"""Out-of-band notification, and an honest account of whether it works.

A broken notifier is worse than an absent one: the system believes it has an
alarm. On 2026-08-14 and again on 2026-08-21 every send returned `403
Forbidden`, logged one warning each, and nothing aggregated those into "the
notification channel is down". Four warnings in an afternoon read as noise —
they were the alarm being dead for the whole session, while UNPROTECTED
POSITION was being routed through it.

So each notifier remembers its last outcome, and `/healthz` reports it. "Is my
alarm working?" should be a field you can look at, not an inference from log
volume.
"""

from __future__ import annotations

import logging

import json
import urllib.request
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class DiscordNotifier:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        self.last_ok: str | None = None
        self.last_error: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url)

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
                self.last_ok = datetime.now(timezone.utc).isoformat()
                self.last_error = None
                return
        except Exception as exc:
            # Recorded as well as logged. A warning per failure never adds up
            # to "the channel is down"; a field does.
            self.last_error = f"{exc}"
            logger.warning("notification failed (%s); continuing", exc)
            return


class NullNotifier:
    """No webhook configured. Says so rather than pretending to be healthy."""

    def __init__(self) -> None:
        self.last_ok: str | None = None
        self.last_error: str | None = None

    @property
    def configured(self) -> bool:
        return False

    def send(self, content: str) -> None:
        return None
