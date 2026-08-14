"""Logging policy, owned by the process rather than by the app factory.

`create_app()` used to set the package logger's level from settings. That made
merely CONSTRUCTING the app mutate global logging state, so an embedding
application or a test that had configured DEBUG found itself silently reset to
INFO. It caught me out in my own demo script within minutes of writing it, and
TradingBot reproduced it:

    logger level before: DEBUG
    Settings.log_level:  INFO
    after create_app():  INFO

A factory should build a thing, not take over the process. So configuration is
explicit and lives here, and the entrypoints call it; anyone embedding the app
keeps their own logging.
"""

from __future__ import annotations

import logging

from .config import Settings


def configure(settings: Settings | None = None) -> None:
    """Apply LOG_LEVEL to this package's logger. Called by entrypoints only."""
    settings = settings or Settings.from_env()
    level = getattr(logging, (settings.log_level or "INFO").upper(), logging.INFO)
    logging.getLogger("tv_alpaca_gateway").setLevel(level)
