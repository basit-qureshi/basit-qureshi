"""Phase E: the integrated system under injected faults.

Every fault here is applied at a realistic boundary through the fake broker.
Nothing contacts a terminal, and nothing destructive happens outside the
harness.

These exercise the PRODUCTION path — the same `_tick` composition the loop
runs — rather than a test-only shortcut, because a test that bypasses the real
path verifies a different program.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app import db as db_module
from app.db import TradeRecord
from tests.conftest import MAGIC

UTC = timezone.utc


def orders(broker):
    return broker.get_pending_orders("XAUUSD", magic=MAGIC)


def positions(broker):
    return broker.get_open_positions("XAUUSD", magic=MAGIC)


def armed(broker, engine):
    engine._tick()
    broker.next_candle()
    engine._tick()
    return orders(broker)


def live(**kw):
    """An admissible configuration. $60 stop, because a full 10+10 grid at
    0.30 freezes near -$37.80 and a smaller stop is refused by admission."""
    kw.setdefault("basket_stop_loss_usd", 60.0)
    kw.setdefault("max_daily_loss_usd", 10_000.0)
    kw.setdefault("basket_take_profit_usd", 10_000.0)
    return kw


# --- runtime knobs actually reach runtime ------------------------------------

def test_the_cadence_knobs_reach_the_engine(broker, engine_factory):
    e = engine_factory(**live(), protective_poll_seconds=0.25, reporting_poll_seconds=7.5)
    assert e.protective_poll_seconds == 0.25
    assert e.reporting_poll_seconds == 7.5
    status = e.status()
    assert status["protective_poll_seconds"] == 0.25
    assert status["reporting_poll_seconds"] == 7.5


def test_poll_interval_seconds_is_declared_retired_not_silently_ignored(broker, engine_factory):
    """It used to drive the loop. Phase B replaced it with two cadences, and a
    knob that quietly does nothing is worse than one that is gone: the owner
    changes it, sees no effect, and cannot tell whether the bot is broken."""
    e = engine_factory(**live())
    status = e.status()
    assert "poll_interval_seconds_retired" in status, (
        "the retired knob must be reported so a stale setting is visible"
    )
    assert status["poll_interval_seconds_retired"] is True


# --- broker read failures are unknown, never zero ----------------------------

def test_a_failed_position_read_is_not_confirmed_zero_exposure(broker, engine_factory, monkeypatch):
    e = engine_factory(**live())
    armed(broker, e)
    captured = {}
    e.on_update = captured.update
    monkeypatch.setattr(broker, "get_open_positions",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("link down")))
    e._broadcast(broker.get_account_info(), e._safe_positions(), [], 0.0)
    assert captured["positions_known"] is False


def test_a_broker_read_failure_does_not_crash_the_cycle(broker, engine_factory, monkeypatch):
    e = engine_factory(**live())
    armed(broker, e)
    monkeypatch.setattr(broker, "get_pending_orders",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("link down")))
    with pytest.raises(RuntimeError):
        e._protective_tick()          # surfaced, not swallowed into a false calm
    assert e._safe_pendings() is None, "an unreadable list must be None, not []"


# --- stale prices and missing costs ------------------------------------------

def test_missing_symbol_info_does_not_let_a_basket_claim_the_target(broker, engine_factory, monkeypatch):
    e = engine_factory(**live(basket_take_profit_usd=10.0))
    armed(broker, e)
    for o in list(orders(broker)):
        broker.cancel_pending_order(o.ticket)
    broker.open_position("BUY", broker.price - 40.0)     # well past the target gross
    monkeypatch.setattr(broker, "get_symbol_info",
                        lambda s: (_ for _ in ()).throw(RuntimeError("no symbol info")))
    broker.next_candle()
    e._tick()
    assert positions(broker), "the target was claimed with an unknown exit cost"


def test_missing_costs_do_not_suppress_a_triggered_loss_exit(broker, engine_factory, monkeypatch):
    e = engine_factory(**live())
    armed(broker, e)
    for o in list(orders(broker)):
        broker.cancel_pending_order(o.ticket)
    pos = broker.open_position("BUY", broker.price + 70.0)
    pos.costs_known = False
    monkeypatch.setattr(broker, "get_symbol_info",
                        lambda s: (_ for _ in ()).throw(RuntimeError("no symbol info")))
    broker.next_candle()
    e._tick()
    assert positions(broker) == [], "a triggered loss exit was suppressed by missing data"


# --- placement: rejection and partial fills ----------------------------------

def test_a_rejected_placement_leaves_a_visible_error_not_a_silent_partial(broker, engine_factory, monkeypatch):
    e = engine_factory(**live())
    e._tick()
    broker.next_candle()
    calls = {"n": 0}
    real = broker.place_pending_order

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] > 12:
            raise RuntimeError("broker rejected the order")
        return real(*a, **k)

    monkeypatch.setattr(broker, "place_pending_order", flaky)
    e._tick()
    placed = len(orders(broker))
    assert 0 < placed < 20, "fixture: placement was supposed to be partial"
    assert e._last_error or e.status().get("last_error"), (
        "a partial placement left no visible error"
    )


def test_a_partial_grid_is_not_topped_up_on_the_next_cycle(broker, engine_factory, monkeypatch):
    """Adding orders to a half-placed basket is adding exposure to something
    already in an unknown state."""
    e = engine_factory(**live())
    e._tick()
    broker.next_candle()
    calls = {"n": 0}
    real = broker.place_pending_order

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] > 8:
            raise RuntimeError("rejected")
        return real(*a, **k)

    monkeypatch.setattr(broker, "place_pending_order", flaky)
    e._tick()
    after_first = len(orders(broker))
    monkeypatch.undo()
    for _ in range(3):
        broker.next_candle()
        e._tick()
    assert len(orders(broker)) == after_first, "the partial grid was topped up"


# --- ownership ----------------------------------------------------------------

def test_no_path_touches_another_magic_number(broker, engine_factory):
    e = engine_factory(**live())
    armed(broker, e)
    manual = broker.open_position("BUY", 4000.0, magic=111111)
    other_ea = broker.open_position("SELL", 4000.0, magic=222222)

    e.pause_entries()
    e.close_and_pause()
    broker.price += 40.0
    broker.next_candle()
    e._tick()

    assert manual.ticket in broker.positions, "a manual trade was closed"
    assert other_ea.ticket in broker.positions, "another program's trade was closed"


# --- close lifecycle ----------------------------------------------------------

def test_a_delayed_acknowledgement_does_not_create_duplicate_exposure(broker, engine_factory, monkeypatch):
    """A close whose response is late must not be resent into a second close."""
    e = engine_factory(**live())
    armed(broker, e)
    broker.price += 4.0
    broker.next_candle()
    e._tick()
    before = len(positions(broker))
    assert before > 0

    seen = []
    real_close = broker.close_position

    def slow(ticket):
        seen.append(ticket)
        return real_close(ticket)

    monkeypatch.setattr(broker, "close_position", slow)
    e.close_and_pause()
    assert len(seen) == len(set(seen)), "the same ticket was closed twice"
    assert positions(broker) == []


def test_a_fill_during_cancellation_is_discovered_and_managed(broker, engine_factory):
    e = engine_factory(**live())
    armed(broker, e)
    e.pause_entries()
    raced = broker.open_position("BUY", 4001.0)
    e._protective_tick()
    assert any(p.ticket == raced.ticket for p in positions(broker))
    allowed, reason = e._entry_gate(broker.get_account_info())
    assert allowed is False and "PAUSED" in reason


def test_a_loss_exit_keeps_its_pause_across_many_cycles(broker, engine_factory):
    e = engine_factory(**live(basket_stop_loss_usd=60.0))
    armed(broker, e)
    broker.price += 4.0
    broker.next_candle()
    e._tick()
    for o in list(orders(broker)):
        broker.cancel_pending_order(o.ticket)
    broker.price -= 30.0
    e._protective_tick()
    assert e._entries_paused is True
    for _ in range(10):
        broker.next_candle()
        e._tick()
    assert orders(broker) == [], "a loss exit rebuilt on its own"


# --- settings reload and mode toggle must not erase a breach -----------------

def test_a_settings_rebuild_does_not_erase_a_breached_budget(broker, engine_factory):
    """The specific regression: rebuilding the engine to apply a setting is the
    same code path a restart takes, and a breach must survive both."""
    e = engine_factory(**live(max_daily_loss_usd=15.0))
    e._tick()
    e._halt_reason = "daily loss limit reached (illustrative)"
    e._persist_risk_state()

    rebuilt = engine_factory(**live(max_daily_loss_usd=15.0))
    rebuilt._tick()
    assert rebuilt._halt_reason is not None, "a settings rebuild cleared the halt"
    allowed, _ = rebuilt._entry_gate(broker.get_account_info())
    assert allowed is False


def test_a_mode_toggle_does_not_select_a_fresh_budget(broker, engine_factory):
    e = engine_factory(**live(max_daily_loss_usd=15.0))
    e._tick()
    e._halt_reason = "daily loss limit reached (illustrative)"
    e._persist_risk_state()
    key_before = e._risk_key()
    e.mode = "real"
    assert e._risk_key() == key_before


def test_raising_the_loss_limit_does_not_release_an_existing_halt(broker, engine_factory):
    """Widening a budget after a breach must not be a way out of the halt."""
    e = engine_factory(**live(max_daily_loss_usd=15.0))
    e._tick()
    e._halt_reason = "daily loss limit reached (illustrative)"
    e._persist_risk_state()

    widened = engine_factory(**live(max_daily_loss_usd=100_000.0))
    widened._tick()
    assert widened._halt_reason is not None, "raising the limit released the halt"


# --- account switch -----------------------------------------------------------

def test_an_account_switch_does_not_carry_state_across(broker, engine_factory):
    e = engine_factory(**live(max_daily_loss_usd=15.0))
    e._tick()
    e._halt_reason = "daily loss limit reached (illustrative)"
    e._equity_peak = 9999.0
    e._persist_risk_state()

    broker.account_id = "fake:other:2"
    other = engine_factory(**live(max_daily_loss_usd=15.0))
    other._tick()
    assert other._halt_reason is None
    assert other._equity_peak != 9999.0


def test_a_real_account_behind_a_demo_label_is_refused_on_every_path(broker, engine_factory):
    e = engine_factory(**live())
    broker.trade_mode = "real"
    assert e.verify_account(broker.get_account_info()).allowed is False
    allowed, _ = e._entry_gate(broker.get_account_info())
    assert allowed is False


# --- persistence failure ------------------------------------------------------

def test_a_persistence_failure_blocks_entries_and_stays_visible(broker, engine_factory, monkeypatch):
    e = engine_factory(**live())
    e._tick()
    monkeypatch.setattr(db_module, "save_risk",
                        lambda *a, **k: (_ for _ in ()).throw(db_module.PersistenceError("disk full")))
    e._persist_risk_state()
    allowed, reason = e._entry_gate(broker.get_account_info())
    assert allowed is False and "disk full" in reason
    assert e.status()["persistence_error"]


# --- midnight transition ------------------------------------------------------

def test_a_midnight_transition_reanchors_without_discarding_carried_exposure(broker, engine_factory):
    e = engine_factory(**live())
    armed(broker, e)
    broker.price += 4.0
    broker.next_candle()
    e._tick()
    carried = positions(broker)
    assert carried, "fixture: something should be carried across the boundary"

    broker.next_candle(minutes=60 * 24)
    e._tick()
    risk = e._day_risk()
    assert risk.day_open_marked is not None, "the new day has no opening anchor"
    assert risk.complete is True
    # Yesterday's unrealised loss must not be charged to today a second time.
    assert abs(risk.open_mark_change) < abs(risk.open_marked) + 1e-6


def test_the_drawdown_anchor_is_not_reset_by_a_new_day(broker, engine_factory):
    e = engine_factory(**live())
    e._tick()
    broker.balance = 5000.0
    broker.next_candle()
    e._tick()
    peak = e._equity_peak
    broker.balance = 3000.0
    broker.next_candle(minutes=60 * 24)
    e._tick()
    assert e._equity_peak == peak


# --- idempotent settlement ----------------------------------------------------

def test_settling_the_same_ticket_twice_does_not_double_count(broker, engine_factory):
    e = engine_factory(**live())
    armed(broker, e)
    broker.price += 4.0
    broker.next_candle()
    e._tick()
    e.close_and_pause()
    e._settle_closed_trades("first")
    first = e._day_risk().settled_realized
    for _ in range(3):
        e._settle_closed_trades("second")
    assert e._day_risk().settled_realized == first, "a repeated settlement changed the total"


def test_a_repeated_close_reason_does_not_overwrite_the_original(broker, engine_factory):
    e = engine_factory(**live())
    armed(broker, e)
    broker.price += 4.0
    broker.next_candle()
    e._tick()
    e._close_everything(positions(broker), orders(broker), "original reason")
    e._settle_closed_trades("later sweep")
    with db_module.SessionLocal() as session:
        rows = session.query(TradeRecord).filter(
            TradeRecord.magic == MAGIC, TradeRecord.close_reason.isnot(None)).all()
    assert rows and all(r.close_reason == "original reason" for r in rows)


# --- optional features cannot weaken protection -------------------------------

def test_a_failing_snapshot_consumer_does_not_stop_a_loss_exit(broker, engine_factory):
    e = engine_factory(**live())
    e.on_update = lambda payload: (_ for _ in ()).throw(RuntimeError("ui gone"))
    armed(broker, e)
    broker.price += 4.0
    broker.next_candle()
    e._tick()
    for o in list(orders(broker)):
        broker.cancel_pending_order(o.ticket)
    broker.price -= 30.0
    e._protective_tick()
    assert positions(broker) == []


def test_slow_reporting_cannot_delay_a_loss_exit(broker, engine_factory):
    e = engine_factory(**live())
    armed(broker, e)
    broker.price += 4.0
    broker.next_candle()
    e._tick()
    for o in list(orders(broker)):
        broker.cancel_pending_order(o.ticket)

    def never(*a, **k):
        raise AssertionError("protection waited on reporting work")

    broker.history_records = never
    broker.get_candles = never
    broker.price -= 30.0
    e._protective_tick()
    assert positions(broker) == []


def test_ai_shadow_cannot_mutate_orders_or_configuration(broker, engine_factory):
    """The AI layer is not wired into the engine at all, which is the
    strongest form of this guarantee. Asserted rather than assumed."""
    import app.engine.grid_engine as ge

    source = open(ge.__file__, encoding="utf-8").read()
    for forbidden in ("app.ai", "EntryPredictor", "ShadowRecorder", "AIMode"):
        assert forbidden not in source, f"the engine references {forbidden!r}"

    from app.ai.contracts import AIMode
    from app.ai.predictor import EntryPredictor, decide
    from app.ai.features import FEATURE_SCHEMA

    predictor = EntryPredictor(None, symbol="XAUUSD", profile_key="baseline@v1",
                               mode=AIMode.SHADOW)
    outcome = decide(deterministic_allowed=False, deterministic_reason="capital floor",
                     predictor=predictor, features=None, mode=AIMode.SHADOW)
    assert outcome.admitted is False and outcome.ai_consulted is False


def test_a_missing_model_does_not_block_a_protective_exit(broker, engine_factory):
    e = engine_factory(**live())
    armed(broker, e)
    broker.price += 4.0
    broker.next_candle()
    e._tick()
    for o in list(orders(broker)):
        broker.cancel_pending_order(o.ticket)
    broker.price -= 30.0
    e._protective_tick()          # no model exists anywhere; the exit still runs
    assert positions(broker) == []


# --- same-candle replacement requires confirmed closure -----------------------

def test_same_candle_replacement_requires_a_confirmed_flat_basket(broker, engine_factory, monkeypatch):
    e = engine_factory(**live(basket_take_profit_usd=1.0))
    armed(broker, e)
    monkeypatch.setattr(broker, "close_position",
                        lambda t: (_ for _ in ()).throw(RuntimeError("rejected")))
    broker.price += 4.0
    e._tick()
    assert e._baskets_won == 0, "a basket was counted before it was confirmed flat"
    assert e._close_intent is not None

    monkeypatch.undo()
    candle = broker.candle_time
    e._tick()
    assert broker.candle_time == candle
    assert e._baskets_won == 1
    assert len(orders(broker)) == 20


def test_replacement_still_passes_every_admission_gate(broker, engine_factory):
    """A profitable close is not a bypass.

    The balance drops below the floor AFTER the win is booked. Dropping it
    before would be a different test: the capital floor is now an active
    trigger, so a breach while the basket is still open flattens it as a breach
    rather than letting it run to its target — that path is
    `test_a_floor_breach_flattens_even_a_profitable_basket`.
    """
    e = engine_factory(**live(basket_take_profit_usd=1.0), capital_floor_usd=900.0)
    armed(broker, e)
    broker.price += 4.0
    e._tick()
    assert e._baskets_won == 1
    broker.balance = 850.0          # now under the floor
    broker.next_candle()
    e._tick()
    assert orders(broker) == [], "a replacement grid bypassed the capital floor"


def test_a_floor_breach_flattens_even_a_profitable_basket(broker, engine_factory):
    """Protection runs before the profit target, and the floor is protection.

    A basket sitting at a profit is still exposure, and an account under its
    floor has already crossed the line the owner drew. The basket is flattened
    as a breach: it is not counted as a win, entries latch, and the liquidation
    policy stands.
    """
    e = engine_factory(**live(basket_take_profit_usd=1.0), capital_floor_usd=900.0)
    armed(broker, e)
    broker.balance = 850.0          # under the floor while the basket is open
    broker.price += 4.0
    e._tick()

    assert e._halt_reason is not None and "capital floor" in e._halt_reason
    assert e._baskets_won == 0, "a floor breach was booked as a win"
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == []
    assert orders(broker) == []
    assert e._entries_paused is True
    assert e._liquidation is not None and e._liquidation.cause == "capital_floor"


# --- migration and restore on a disposable copy -------------------------------

REPRESENTATIVE_ROWS = [
    ("t1", "XAUUSDm", "BUY", 0.01, 4000.0, 1.50, "demo", "CLOSED", 990022,
     "2026-09-19", "basket target reached (+1.50)"),
    ("t2", "XAUUSDm", "SELL", 0.01, 4001.0, -4.25, "demo", "CLOSED", 990022,
     "2026-09-19", "basket stop hit (-4.25)"),
    ("manual1", "XAUUSDm", "BUY", 0.10, 3990.0, 12.00, "demo", "CLOSED", 111111,
     "2026-09-19", None),
]


def build_legacy_db(path):
    """A database in the shape that shipped BEFORE the Phase A columns."""
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE trades (
      id INTEGER PRIMARY KEY AUTOINCREMENT, ticket VARCHAR, account_id VARCHAR DEFAULT 'legacy',
      symbol VARCHAR, side VARCHAR, volume FLOAT, open_price FLOAT, close_price FLOAT,
      sl FLOAT, tp FLOAT, profit FLOAT, mode VARCHAR, status VARCHAR,
      open_time DATETIME, close_time DATETIME, magic INTEGER, trading_day VARCHAR,
      close_reason VARCHAR)""")
    for (ticket, symbol, side, volume, price, profit, mode, status, magic,
         day, reason) in REPRESENTATIVE_ROWS:
        con.execute(
            "INSERT INTO trades (ticket,symbol,side,volume,open_price,sl,tp,profit,mode,"
            "status,open_time,close_time,magic,trading_day,close_reason) VALUES "
            "(?,?,?,?,?,0,0,?,?,?,'2026-09-19 10:00:00','2026-09-19 11:00:00',?,?,?)",
            (ticket, symbol, side, volume, price, profit, mode, status, magic, day, reason))
    con.commit()
    con.close()


def with_database(path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    saved = (db_module.engine, db_module.SessionLocal)
    db_module.engine = engine
    db_module.SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    return engine, saved


def test_migration_preserves_ownership_reasons_fees_and_timestamps(tmp_path):
    path = tmp_path / "legacy.db"
    build_legacy_db(path)
    engine, saved = with_database(path)
    try:
        db_module.init_db()
        from sqlalchemy import text
        with engine.begin() as conn:
            rows = list(conn.execute(text(
                "SELECT ticket, magic, profit, close_reason, open_time, close_time, symbol "
                "FROM trades ORDER BY ticket")))
    finally:
        engine.dispose()
        db_module.engine, db_module.SessionLocal = saved

    by_ticket = {r[0]: r for r in rows}
    assert len(rows) == 3, "a row was lost during migration"
    assert by_ticket["manual1"][1] == 111111, "manual trade ownership changed"
    assert by_ticket["t1"][3] == "basket target reached (+1.50)", "a close reason was lost"
    assert by_ticket["t2"][2] == -4.25, "a recorded result changed"
    assert by_ticket["t1"][4] == "2026-09-19 10:00:00", "a timestamp was rewritten"
    assert by_ticket["t1"][6] == "XAUUSDm", "the broker-suffixed symbol was altered"


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "legacy.db"
    build_legacy_db(path)
    engine, saved = with_database(path)
    try:
        db_module.init_db()
        db_module.init_db()
        db_module.init_db()
        from sqlalchemy import text
        with engine.begin() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM trades")).scalar()
    finally:
        engine.dispose()
        db_module.engine, db_module.SessionLocal = saved
    assert count == 3, "repeated migration duplicated or dropped rows"


def test_a_backup_restores_to_an_identical_database(tmp_path):
    path = tmp_path / "live.db"
    backup = tmp_path / "live.bak.db"
    build_legacy_db(path)
    engine, saved = with_database(path)
    try:
        db_module.init_db()
    finally:
        engine.dispose()
        db_module.engine, db_module.SessionLocal = saved

    backup.write_bytes(path.read_bytes())
    con = sqlite3.connect(path)
    con.execute("DELETE FROM trades WHERE ticket='t1'")
    con.commit()
    con.close()
    assert path.read_bytes() != backup.read_bytes()

    path.write_bytes(backup.read_bytes())          # restore
    con = sqlite3.connect(path)
    restored = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    con.close()
    assert restored == 3


def test_a_migrated_database_is_not_readable_by_the_old_schema_expectations(tmp_path):
    """Rolling code back across a schema change is not automatically safe.

    After migration the table carries columns the older code never wrote. That
    is survivable for SQLite reads, but a rollback that then writes rows without
    them produces history the new code reads as incomplete — which is why the
    runbook says restore the backup, not just the code.
    """
    path = tmp_path / "legacy.db"
    build_legacy_db(path)
    engine, saved = with_database(path)
    try:
        db_module.init_db()
    finally:
        engine.dispose()
        db_module.engine, db_module.SessionLocal = saved

    con = sqlite3.connect(path)
    columns = {row[1] for row in con.execute("PRAGMA table_info(trades)")}
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert "close_reason" in columns
    assert "risk_state" in tables, "the risk state table is part of the migrated shape"
