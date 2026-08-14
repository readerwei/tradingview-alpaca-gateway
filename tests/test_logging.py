from __future__ import annotations

import logging

import pytest

from tv_alpaca_gateway.config import Settings, configure_logging


def test_log_level_is_read_from_environment(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert Settings.from_env().log_level == "DEBUG"


def test_invalid_log_level_is_refused():
    with pytest.raises(ValueError, match="LOG_LEVEL"):
        Settings(log_level="LOUD").validate()


def test_configure_logging_enables_debug(monkeypatch):
    root = logging.getLogger()
    previous = root.level
    try:
        configure_logging("DEBUG")
        assert root.level == logging.DEBUG
    finally:
        root.setLevel(previous)
