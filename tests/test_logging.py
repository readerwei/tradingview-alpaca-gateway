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


# ═══════════════════ gaps left after the first observability pass

def test_the_gateway_entrypoint_honours_log_level():
    """`configure_logging` was called from direct_runner but not from
    `main.run()`, so LOG_LEVEL had no effect on the gateway itself — the
    process it matters most for.

    A knob that silently does nothing is worse than no knob: you turn it, see
    no change, and conclude the thing you were debugging is not the problem.
    """
    import inspect

    from tv_alpaca_gateway import main

    assert "configure_logging" in inspect.getsource(main.run)


def test_the_heartbeat_reports_state_with_no_events(tmp_path, caplog):
    """Event-driven logging cannot show a stalled process by construction —
    no events, no lines. Two days were lost to exactly that ambiguity."""
    import logging
    from decimal import Decimal

    from tv_alpaca_gateway import exit_manager as m
    from tv_alpaca_gateway.broker import FakeBroker
    from tv_alpaca_gateway.lot_supervisor import LotSupervisor
    from tv_alpaca_gateway.store import EventStore

    broker = FakeBroker()
    broker.positions["BTC/USD"] = Decimal("0.0015")
    sup = LotSupervisor(EventStore(tmp_path / "h.sqlite3"), broker)
    sup.adopt(m.open_lot(m.Lot.opened(
        event_id="hb", symbol="BTC/USD", entry_price=Decimal("64000"),
        initial_stop=Decimal("63800"), held_qty=Decimal("0.0015"), timeframe="1m",
        plan=m.ExitPlan(name="P", tranches=((Decimal("0.2"), Decimal("1.2")),),
                        runner_fraction=Decimal("0.8"),
                        trail_source="previous_completed_bar_low", breakeven_after=1),
        min_order_size=Decimal("0.000015437")), broker))

    with caplog.at_level(logging.INFO, logger="tv_alpaca_gateway"):
        sup.heartbeat()

    assert "heartbeat" in caplog.text and "working_stop" in caplog.text


def test_an_empty_heartbeat_still_says_so(tmp_path, caplog):
    """"No open lots" is information. Printing nothing is not."""
    import logging

    from tv_alpaca_gateway.broker import FakeBroker
    from tv_alpaca_gateway.lot_supervisor import LotSupervisor
    from tv_alpaca_gateway.store import EventStore

    sup = LotSupervisor(EventStore(tmp_path / "h2.sqlite3"), FakeBroker())
    with caplog.at_level(logging.INFO, logger="tv_alpaca_gateway"):
        sup.heartbeat()

    assert "no open lots" in caplog.text


def test_debug_says_how_far_the_next_target_is(tmp_path, caplog):
    """State alone does not answer "why did nothing fire". The distance does."""
    import logging
    from decimal import Decimal

    from tv_alpaca_gateway import exit_manager as m
    from tv_alpaca_gateway.broker import FakeBroker

    broker = FakeBroker()
    broker.positions["BTC/USD"] = Decimal("0.0015")
    lot = m.open_lot(m.Lot.opened(
        event_id="d", symbol="BTC/USD", entry_price=Decimal("64000"),
        initial_stop=Decimal("63800"), held_qty=Decimal("0.0015"), timeframe="1m",
        plan=m.ExitPlan(name="P", tranches=((Decimal("0.2"), Decimal("1.2")),),
                        runner_fraction=Decimal("0.8"),
                        trail_source="previous_completed_bar_low", breakeven_after=1),
        min_order_size=Decimal("0.000015437")), broker)

    with caplog.at_level(logging.DEBUG, logger="tv_alpaca_gateway"):
        lot.on_price(Decimal("64050"))

    assert "tp1" in caplog.text, "no distance to the next target was logged"
