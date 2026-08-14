from __future__ import annotations

import logging

import pytest

import pytest

from tv_alpaca_gateway.config import Settings, configure_logging


def test_log_level_is_read_from_environment(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert Settings.from_env().log_level == "DEBUG"


def test_invalid_log_level_is_refused():
    with pytest.raises(ValueError, match="LOG_LEVEL"):
        Settings(log_level="LOUD").validate()


def test_configure_logging_enables_debug(monkeypatch):
    """UPDATED: DEBUG now applies to this package, not to the root logger.

    The original asserted `root.level == DEBUG`, which was the behaviour when
    written. Scoping it changed that deliberately — root at DEBUG turns on every
    websockets frame and all of asyncio, burying the one line you turned DEBUG on
    to read. The intent this test protects is unchanged: LOG_LEVEL=DEBUG must
    actually produce our debug output.
    """
    root = logging.getLogger()
    ours = logging.getLogger("tv_alpaca_gateway")
    previous = (root.level, ours.level)
    try:
        configure_logging("DEBUG")
        assert ours.level == logging.DEBUG
        assert ours.isEnabledFor(logging.DEBUG), "our debug records would be dropped"
    finally:
        root.setLevel(previous[0])
        ours.setLevel(previous[1])


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

    lot.on_price(Decimal("64050"))
    from tv_alpaca_gateway.lot_supervisor import LotSupervisor
    from tv_alpaca_gateway.store import EventStore

    supervisor = LotSupervisor(EventStore(tmp_path / "h3.sqlite3"), broker)
    supervisor.adopt(lot)
    with caplog.at_level(logging.INFO, logger="tv_alpaca_gateway"):
        # The distance is intentionally heartbeat output, not a per-trade dump.
        supervisor.heartbeat()

    assert "tp1" in caplog.text, "no distance to the next target was logged"


def test_debug_is_scoped_to_this_package_not_the_whole_process():
    """LOG_LEVEL=DEBUG is asked for when OUR decisions are unclear.

    Setting the root logger turns on every websockets frame, every urllib3
    connection and the whole of asyncio alongside them. The line explaining why
    a rung did not fire is then present but unfindable — which is the same
    failure as not logging it at all.
    """
    import logging

    from tv_alpaca_gateway.config import configure_logging

    root, ours = logging.getLogger(), logging.getLogger("tv_alpaca_gateway")
    before = (root.level, ours.level)
    try:
        configure_logging("DEBUG")
        assert ours.level == logging.DEBUG, "our own logger was not turned up"
        assert root.level > logging.DEBUG, (
            "the root logger was set to DEBUG; third-party frames will bury ours")
    finally:
        root.setLevel(before[0])
        ours.setLevel(before[1])


def test_third_party_warnings_are_still_visible():
    """Quieter, not deafened — a websockets error still has to reach the log."""
    import logging

    from tv_alpaca_gateway.config import configure_logging

    root = logging.getLogger()
    before = root.level
    try:
        configure_logging("DEBUG")
        assert root.level <= logging.INFO, "third-party INFO and above were suppressed"
    finally:
        root.setLevel(before)


def test_quote_only_bars_do_not_flood_the_package_logger(tmp_path, caplog):
    """Measured on master before this change: 50 quote-only bars produced 50
    package-logger lines.

    #51 fixed the trade paths and left this one. It matters more than it looks:
    59% of Alpaca's BTC/USD 1m bars contain no trade, so this is the most
    frequent bar line there is.
    """
    import logging
    from decimal import Decimal

    from tv_alpaca_gateway import exit_manager as m
    from tv_alpaca_gateway.broker import FakeBroker
    from tv_alpaca_gateway.config import configure_logging

    configure_logging("DEBUG", log_market_data=False)
    broker = FakeBroker()
    broker.positions["BTC/USD"] = Decimal("0.0015")
    lot = m.open_lot(m.Lot.opened(
        event_id="q", symbol="BTC/USD", entry_price=Decimal("64000"),
        initial_stop=Decimal("63800"), held_qty=Decimal("0.0015"), timeframe="1m",
        plan=m.ExitPlan(name="P", tranches=((Decimal("0.2"), Decimal("1.2")),),
                        runner_fraction=Decimal("0.8"),
                        trail_source="previous_completed_bar_low", breakeven_after=1),
        min_order_size=Decimal("0.000015437")), broker)

    with caplog.at_level(logging.DEBUG, logger="tv_alpaca_gateway"):
        # Arming the lot legitimately logs one action line per broker call.
        # This test is about what the MESSAGES produce, so the setup noise is
        # discarded rather than counted — otherwise it would fail for the one
        # kind of logging we do want.
        caplog.clear()
        for _ in range(50):
            lot.on_bar(high=Decimal("64100"), low=Decimal("64050"),
                       close=Decimal("64080"), trade_count=0)

    noisy = [r for r in caplog.records
             if not r.name.startswith("tv_alpaca_gateway.marketdata")]
    assert not noisy, f"50 quote-only bars produced {len(noisy)} package-logger lines"




def test_no_message_type_floods_the_package_logger(tmp_path, caplog):
    """Every inbound message type, measured rather than inferred.

    I first wrote this as a static scan of the handler source and it produced
    false positives twice — it cannot see that a line is guarded by a
    state-change check, so it flagged precisely the lines #51 had already
    fixed correctly. Two rounds of that is enough. The property is "a quiet
    feed produces quiet logs", so measure that instead of guessing at it from
    syntax.

    Measured on master before this change:

        60 traded bars, no state change  ->  60 lines
        60 quote-only bars               -> 120 lines
        60 bars on an unwatched symbol   ->  60 lines
    """
    import logging
    from decimal import Decimal

    from tv_alpaca_gateway import exit_manager as m
    from tv_alpaca_gateway.broker import FakeBroker
    from tv_alpaca_gateway.config import configure_logging
    from tv_alpaca_gateway.lot_supervisor import LotSupervisor
    from tv_alpaca_gateway.store import EventStore

    configure_logging("DEBUG", log_market_data=False)
    broker = FakeBroker()
    broker.positions["BTC/USD"] = Decimal("0.0015")
    sup = LotSupervisor(EventStore(tmp_path / "f.sqlite3"), broker)
    sup.adopt(m.open_lot(m.Lot.opened(
        event_id="f", symbol="BTC/USD", entry_price=Decimal("64000"),
        initial_stop=Decimal("63800"), held_qty=Decimal("0.0015"), timeframe="1m",
        plan=m.ExitPlan(name="P", tranches=((Decimal("0.2"), Decimal("1.2")),),
                        runner_fraction=Decimal("0.8"),
                        trail_source="previous_completed_bar_low", breakeven_after=1),
        min_order_size=Decimal("0.000015437")), broker))

    def _bar(symbol="BTC/USD", trades=3):
        return type("B", (), {"symbol": symbol, "high": Decimal("64100"),
                              "low": Decimal("64050"), "close": Decimal("64080"),
                              "trade_count": trades, "timestamp": "t"})()

    def _trade(symbol="BTC/USD", price=63900.0):
        return type("T", (), {"symbol": symbol, "price": price, "timestamp": "t"})()

    cases = {
        "traded bars, no state change": lambda: sup.on_bar(_bar()),
        "quote-only bars": lambda: sup.on_bar(_bar(trades=0)),
        "bars on an unwatched symbol": lambda: sup.on_bar(_bar(symbol="ETH/USD")),
        "trades at one price": lambda: sup.on_trade(_trade()),
        "trades on an unwatched symbol": lambda: sup.on_trade(_trade(symbol="ETH/USD")),
    }
    noisy = {}
    for name, send in cases.items():
        with caplog.at_level(logging.DEBUG, logger="tv_alpaca_gateway"):
            caplog.clear()
            for _ in range(60):
                send()
            count = len([r for r in caplog.records
                         if not r.name.startswith("tv_alpaca_gateway.marketdata")])
        if count:
            noisy[name] = count

    assert not noisy, f"60 messages each produced package-logger lines: {noisy}"


# ═══════════════ what the gateway DID, not only what the broker said

def test_the_ladder_narrates_its_own_actions(tmp_path, caplog):
    """Wei: "things that should be logged but were not."

    Every order line in the 2026-08-14 log came from Alpaca's trade_updates
    stream, so it showed what the broker happened to tell us rather than what
    the gateway decided to do. Three cancels appeared and one placement,
    because the stream was being torn down after each message — and the
    resize-before-sell ordering, the single decision this design rests on,
    produced no line of its own at all.

    A reader must be able to reconstruct the sequence from our log alone,
    without the broker and without arithmetic on timestamps.
    """
    import logging
    from decimal import Decimal

    from tv_alpaca_gateway import exit_manager as m
    from tv_alpaca_gateway.broker import FakeBroker

    broker = FakeBroker()
    broker.positions["TSLA"] = Decimal("10")
    plan = m.ExitPlan(name="P", tranches=((Decimal("0.2"), Decimal("0.2")),),
                      runner_fraction=Decimal("0.8"),
                      trail_source="previous_completed_bar_low", breakeven_after=1)

    with caplog.at_level(logging.INFO, logger="tv_alpaca_gateway"):
        lot = m.open_lot(m.Lot.opened(
            event_id="evt", symbol="TSLA", entry_price=Decimal("340.76"),
            initial_stop=Decimal("340.50"), held_qty=Decimal("10"),
            timeframe="1m", plan=plan, min_order_size=Decimal("1")), broker)
        lot.on_price(Decimal("340.83"))
        lot.on_fill(rung=1, filled_qty=lot.tranche_qty(1), fill_id="a")

    text = caplog.text
    for expected in ("protection", "reserved", "rung 1", "submitted",
                     "complete", "breakeven"):
        assert expected in text, f"the log never mentions {expected!r}"


def test_the_resize_is_narrated_before_the_exit(caplog):
    """The ordering itself has to be readable, not just correct — otherwise a
    future inversion looks exactly like today's log."""
    import logging
    from decimal import Decimal

    from tv_alpaca_gateway import exit_manager as m
    from tv_alpaca_gateway.broker import FakeBroker

    broker = FakeBroker()
    broker.positions["TSLA"] = Decimal("10")
    plan = m.ExitPlan(name="P", tranches=((Decimal("0.2"), Decimal("0.2")),),
                      runner_fraction=Decimal("0.8"),
                      trail_source="previous_completed_bar_low", breakeven_after=1)
    lot = m.open_lot(m.Lot.opened(
        event_id="evt", symbol="TSLA", entry_price=Decimal("340.76"),
        initial_stop=Decimal("340.50"), held_qty=Decimal("10"),
        timeframe="1m", plan=plan, min_order_size=Decimal("1")), broker)

    with caplog.at_level(logging.INFO, logger="tv_alpaca_gateway"):
        caplog.clear()
        lot.on_price(Decimal("340.83"))

    lines = [r.getMessage() for r in caplog.records]
    resize = next(i for i, m_ in enumerate(lines) if "protection 10 -> 8" in m_)
    exit_ = next(i for i, m_ in enumerate(lines) if "submitted" in m_)
    assert resize < exit_, f"the log shows the exit before the resize: {lines}"


def test_a_risk_refusal_says_why(caplog):
    """A refusal that only raises tells the caller; a refusal that logs tells
    whoever reads the log afterwards, which is usually who needs it."""
    import logging
    from decimal import Decimal

    from tv_alpaca_gateway import execution
    from tv_alpaca_gateway.config import Settings
    from tv_alpaca_gateway.pine_alert_parser import parse_pine_alert
    from tv_alpaca_gateway.store import EventStore

    import tempfile, pathlib as _p
    tmp = _p.Path(tempfile.mkdtemp())
    settings = Settings(paper_trading=True, trading_enabled=True, webhook_secret="s",
                        allowed_symbols=frozenset({"QQQ"}), max_qty=1,
                        crypto_max_qty=Decimal("0.05"), max_notional=100000.0,
                        market_symbols=("QQQ",), db_path=tmp / "r.sqlite3")
    alert = ("EXECUTE_ALPACA_ORDER | SYMBOL=QQQ | SIDE=BUY | QTY=999 | "
             "ORDER_TYPE=MARKET | TIME_IN_FORCE=DAY")

    class _B:
        def latest_trade_price(self, _s): return 700.0

    with caplog.at_level(logging.WARNING, logger="tv_alpaca_gateway"):
        with pytest.raises(execution.ExecutionError):
            execution.execute_pine_command(parse_pine_alert(alert), settings, _B(),
                                           EventStore(settings.db_path), delivery_id="d")

    assert "quantity exceeds" in caplog.text
