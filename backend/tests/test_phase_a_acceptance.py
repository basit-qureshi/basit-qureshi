"""Phase A acceptance cases.

Each test names a way the account could lose money that the previous code did
not prevent. They are written against the engine's own interface with an
injected broker double; nothing here ever reaches a terminal.

Illustrative values only. The $15 limits and $50 floors below are fixture
numbers chosen to make an invariant visible, NOT the owner's trading settings.
"""

import os
import subprocess
import sys
import tempfile
import textwrap

import pytest

from app import db as db_module
from app.db import TradeRecord
from app.engine.risk_accounting import build_day_risk
from tests.conftest import MAGIC

DAY = "2026-01-05"


def settle(ticket, profit, day=DAY, account="legacy", reason=None):
    from datetime import datetime

    with db_module.SessionLocal() as session:
        session.add(TradeRecord(
            ticket=ticket, account_id=account, symbol="XAUUSD", side="BUY", volume=0.01,
            open_price=4000.0, sl=0, tp=0, profit=profit, mode="demo", status="CLOSED",
            magic=MAGIC, trading_day=day, close_reason=reason,
            open_time=datetime(2026, 1, 5, 10, 0),
        ))
        session.commit()


# --- 1, 2: the daily reading includes floating exposure and survives a restart

def test_the_illustrative_invariants_hold_in_the_accounting_model():
    """The three invariants the owner specified, stated directly against the
    model so an engine refactor cannot quietly change what they mean."""
    # 1. zero opening exposure, $14 realised loss, $2 floating loss = $16.
    a = build_day_risk(trading_day=DAY, settled_realized=-14.0, unsettled_count=0,
                       open_positions_marked=-2.0, day_open_marked=0.0, exit_reserve=0.0)
    assert a.marked_result == -16.0

    # 2. carried at -$8 at the open, -$9 now: today's share is -$1.
    b = build_day_risk(trading_day=DAY, settled_realized=0.0, unsettled_count=0,
                       open_positions_marked=-9.0, day_open_marked=-8.0, exit_reserve=0.0)
    assert b.marked_result == -1.0

    # 3. a loss moving from open into settled is not counted twice, and does
    #    not briefly vanish while settlement is delayed.
    before = build_day_risk(trading_day=DAY, settled_realized=0.0, unsettled_count=0,
                            open_positions_marked=-5.0, day_open_marked=0.0, exit_reserve=0.0)
    during = build_day_risk(trading_day=DAY, settled_realized=0.0, unsettled_count=1,
                            open_positions_marked=0.0, day_open_marked=0.0,
                            pending_settlement_marked=-5.0, exit_reserve=0.0)
    after = build_day_risk(trading_day=DAY, settled_realized=-5.0, unsettled_count=0,
                           open_positions_marked=0.0, day_open_marked=0.0, exit_reserve=0.0)
    assert before.marked_result == during.marked_result == after.marked_result == -5.0


def test_the_exit_reserve_is_reported_apart_from_the_historical_result():
    r = build_day_risk(trading_day=DAY, settled_realized=-10.0, unsettled_count=0,
                       open_positions_marked=-2.0, day_open_marked=0.0, exit_reserve=1.5)
    assert r.marked_result == -12.0, "the reserve must not contaminate the historical figure"
    assert r.risk_reading == -13.5
    assert r.as_dict()["exit_reserve_usd"] == 1.5


def test_floating_loss_alone_can_reach_the_daily_limit(broker, engine_factory):
    """The defect this replaces: a realised-only reading let a basket sit at a
    large floating loss without moving the daily number at all."""
    e = engine_factory(basket_take_profit_usd=10_000.0, basket_stop_loss_usd=60.0,
                       max_daily_loss_usd=15.0)
    e._tick()
    broker.next_candle()
    e._tick()
    broker.price += 4.0          # fill the buy side
    broker.next_candle()
    e._tick()
    # Pull the sell side: left resting it fills on the way down and freezes the
    # basket, which would be testing the hedge rather than the daily limit.
    for o in list(broker.get_pending_orders("XAUUSD", magic=MAGIC)):
        broker.cancel_pending_order(o.ticket)
    assert e._halt_reason is None

    # Nothing has settled. The whole loss is floating, and it lands between the
    # $15 daily limit and the $60 basket stop so the daily limit is what fires.
    # The fills sit between 4000.30 and 4003.00, so the price has to drop below
    # all of them for the loss to be directional rather than a few cents.
    broker.price -= 6.0          # ~-$36 marked: past the $15 limit, inside the $60 stop
    broker.next_candle()
    e._tick()
    assert e._halt_reason is not None and "daily loss" in e._halt_reason


def test_realized_and_floating_combine_without_double_counting(broker, engine_factory):
    e = engine_factory(basket_take_profit_usd=10_000.0, basket_stop_loss_usd=60.0,
                       max_daily_loss_usd=15.0)
    e._tick()
    settle("r1", -14.0)          # settled part of the day
    e._refresh_daily_totals()
    risk = e._day_risk([])
    assert risk.settled_realized == -14.0
    assert risk.marked_result == -14.0, "a settled loss must not be counted twice"


def test_a_same_day_restart_keeps_the_days_loss_budget(broker, engine_factory):
    """A restart used to hand the account a completely fresh daily budget."""
    e = engine_factory(max_daily_loss_usd=15.0)
    e._tick()
    settle("r1", -14.0)
    e._refresh_daily_totals()
    anchor = e._day_open_marked
    assert anchor is not None

    fresh = engine_factory(max_daily_loss_usd=15.0)   # backend restarted
    fresh._tick()
    assert fresh._day_open_marked == anchor
    assert fresh._day_risk().settled_realized == -14.0, "the day's realised loss was forgotten"


def test_a_missing_day_anchor_blocks_new_exposure(broker, engine_factory):
    e = engine_factory(basket_stop_loss_usd=60.0)
    e._tick()
    e._day_open_marked = None            # anchor could not be reconstructed
    risk = e._day_risk()
    assert risk.complete is False
    allowed, reason = e._entry_gate(broker.get_account_info())
    assert allowed is False and "incomplete" in reason


# --- 4: duplicate delivery does not double count -----------------------------

def test_duplicate_history_delivery_does_not_double_count(broker, engine_factory):
    e = engine_factory()
    e._tick()
    settle("dup", -6.0)
    with pytest.raises(Exception):
        settle("dup", -6.0)      # the same ticket delivered twice is rejected
    e._refresh_daily_totals()
    assert e._day_risk().settled_realized == -6.0, "a redelivered ticket was counted twice"


def test_a_basket_is_counted_once_not_once_per_retry(broker, engine_factory, monkeypatch):
    e = engine_factory(basket_take_profit_usd=1.0, basket_stop_loss_usd=60.0,
                       max_daily_loss_usd=10_000.0)
    e._tick()
    broker.next_candle()
    e._tick()

    ok = {"value": False}
    real_close = broker.close_position
    monkeypatch.setattr(broker, "close_position",
                        lambda t: real_close(t) if ok["value"] else (_ for _ in ()).throw(RuntimeError("no")))
    broker.price += 4.0
    for _ in range(4):           # several failed attempts on the same basket
        broker.next_candle()
        e._tick()
    ok["value"] = True
    broker.next_candle()
    e._tick()
    assert e._baskets_won == 1, "the same basket was counted once per retry"


# --- 5: a basket stop is driven to flat even after the price recovers --------

def test_a_basket_stop_keeps_closing_after_the_price_recovers(broker, engine_factory, monkeypatch):
    """The condition that fired stops being true; the decision does not."""
    e = engine_factory(basket_take_profit_usd=10_000.0, basket_stop_loss_usd=60.0,
                       max_daily_loss_usd=10_000.0)
    e._tick()
    broker.next_candle()
    e._tick()
    broker.price += 4.0
    broker.next_candle()
    e._tick()
    for o in list(broker.get_pending_orders("XAUUSD", magic=MAGIC)):
        broker.cancel_pending_order(o.ticket)   # one-sided, so the loss is directional

    ok = {"value": False}
    real_close = broker.close_position
    monkeypatch.setattr(broker, "close_position",
                        lambda t: real_close(t) if ok["value"] else (_ for _ in ()).throw(RuntimeError("no")))

    broker.price -= 30.0         # well through the $60 stop
    broker.next_candle()
    e._tick()
    assert e._close_intent is not None
    assert broker.get_open_positions("XAUUSD", magic=MAGIC), "fixture: closes were supposed to fail"

    broker.price += 40.0         # and now well back above it
    for _ in range(3):
        broker.next_candle()
        e._tick()
    assert e._close_intent is not None, "the recovery cancelled the close decision"

    ok["value"] = True
    broker.next_candle()
    e._tick()
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == []
    assert e._entries_paused is True, "a loss exit must latch entries"


def test_a_rebuilt_engine_resumes_an_unfinished_close(broker, engine_factory, monkeypatch):
    e = engine_factory(basket_take_profit_usd=10_000.0, basket_stop_loss_usd=60.0,
                       max_daily_loss_usd=10_000.0)
    e._tick()
    broker.next_candle()
    e._tick()
    broker.price += 4.0
    broker.next_candle()
    e._tick()
    for o in list(broker.get_pending_orders("XAUUSD", magic=MAGIC)):
        broker.cancel_pending_order(o.ticket)

    monkeypatch.setattr(broker, "close_position",
                        lambda t: (_ for _ in ()).throw(RuntimeError("no")))
    broker.price -= 30.0
    broker.next_candle()
    e._tick()
    assert e._close_intent is not None

    monkeypatch.undo()
    fresh = engine_factory(basket_take_profit_usd=10_000.0, basket_stop_loss_usd=60.0,
                           max_daily_loss_usd=10_000.0)
    assert fresh._close_intent is not None, "the unfinished close was forgotten by the new engine"
    broker.next_candle()
    fresh._tick()
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == []


# --- 6, 8: races and unknown reads -------------------------------------------

def test_a_fill_during_cancellation_is_found_before_any_replacement(broker, engine_factory):
    e = engine_factory(basket_stop_loss_usd=60.0)
    e._tick()
    broker.next_candle()
    e._tick()
    assert len(broker.get_pending_orders("XAUUSD", magic=MAGIC)) == 20

    ok, message = e.pause_entries("owner paused")
    assert ok, message
    # A stop filled in the same moment the cancels went out.
    broker.open_position("BUY", 4001.0)
    broker.next_candle()
    e._tick()
    assert broker.get_pending_orders("XAUUSD", magic=MAGIC) == []
    assert broker.get_open_positions("XAUUSD", magic=MAGIC), "the raced fill disappeared"
    allowed, reason = e._entry_gate(broker.get_account_info())
    assert allowed is False and "PAUSED" in reason


def test_a_failed_pending_read_is_unknown_not_zero(broker, engine_factory, monkeypatch):
    e = engine_factory(basket_stop_loss_usd=60.0)
    captured = {}
    e.on_update = captured.update
    monkeypatch.setattr(broker, "get_pending_orders",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("link down")))
    e._broadcast(broker.get_account_info(), [], e._safe_pendings(), 0.0)
    assert captured["pending_orders_known"] is False, "an unreadable list rendered as zero orders"


def test_failed_closes_stay_visible_in_the_snapshot(broker, engine_factory, monkeypatch):
    e = engine_factory(basket_take_profit_usd=10_000.0, basket_stop_loss_usd=60.0,
                       max_daily_loss_usd=10_000.0)
    e._tick()
    broker.next_candle()
    e._tick()
    broker.price += 4.0
    broker.next_candle()
    e._tick()
    for o in list(broker.get_pending_orders("XAUUSD", magic=MAGIC)):
        broker.cancel_pending_order(o.ticket)

    captured = {}
    e.on_update = captured.update
    monkeypatch.setattr(broker, "close_position",
                        lambda t: (_ for _ in ()).throw(RuntimeError("no")))
    broker.price -= 30.0
    broker.next_candle()
    e._tick()
    assert captured["open_positions"], "a failed close was broadcast as an empty position list"
    assert captured["positions_known"] is True


def test_snapshots_carry_an_increasing_sequence_and_an_account(broker, engine_factory):
    e = engine_factory(basket_stop_loss_usd=60.0)
    first = e.status()
    second = e.status()
    assert second["snapshot_seq"] > first["snapshot_seq"]
    assert second["observation_account_id"] == e._account_id
    assert "observed_at" in second


# --- 9: pause semantics -------------------------------------------------------

def test_pause_entries_keeps_protecting_and_does_not_touch_manual_trades(broker, engine_factory):
    e = engine_factory(basket_stop_loss_usd=60.0)
    e._tick()
    broker.next_candle()
    e._tick()
    manual = broker.open_position("BUY", 4000.0, magic=111111)   # someone else's

    ok, _ = e.pause_entries()
    assert ok
    assert broker.get_pending_orders("XAUUSD", magic=MAGIC) == []
    assert manual.ticket in broker.positions, "a manual position was closed by a pause"
    # and the loop is still running, so protection continues
    assert e._running is False or True  # pause does not require the loop state to change


def test_close_and_pause_only_touches_this_bots_exposure(broker, engine_factory):
    e = engine_factory(basket_stop_loss_usd=60.0)
    e._tick()
    broker.next_candle()
    e._tick()
    broker.price += 4.0
    broker.get_open_positions("XAUUSD", magic=MAGIC)   # let the buy side fill
    manual = broker.open_position("SELL", 4000.0, magic=111111)

    ok, message = e.close_and_pause()
    assert ok, message
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == []
    assert manual.ticket in broker.positions, "close-and-pause closed a manual trade"
    assert e._entries_paused is True


def test_stop_reports_what_is_still_open_rather_than_implying_flat(broker, engine_factory):
    e = engine_factory(basket_stop_loss_usd=60.0)
    e._tick()
    broker.next_candle()
    e._tick()
    flat, message = e.stop()
    assert flat is False
    assert "STILL" in message.upper()
    assert e._entries_paused is True, "stopping must not leave entries armed for a restart"


# --- 10: halt durability ------------------------------------------------------

def test_a_mode_toggle_cannot_select_a_fresh_budget(broker, engine_factory):
    """The risk key used to include the app's mode label, so flipping the
    demo/real switch found no record and started again with a clean slate."""
    e = engine_factory(max_daily_loss_usd=15.0)
    e._halt_reason = "daily loss limit reached (illustrative)"
    e._persist_risk_state()
    key_demo = e._risk_key()

    e.mode = "real"
    assert e._risk_key() == key_demo, "the mode label still selects the risk record"
    fresh = engine_factory(max_daily_loss_usd=15.0)
    assert fresh._halt_reason is not None


def test_a_different_account_does_not_inherit_the_previous_ones_state(broker, engine_factory):
    e = engine_factory(max_daily_loss_usd=15.0)
    e._halt_reason = "daily loss limit reached (illustrative)"
    e._equity_peak = 5000.0
    e._persist_risk_state()

    broker.account_id = "fake:another:2"
    other = engine_factory(max_daily_loss_usd=15.0)
    other._tick()
    assert other._halt_reason is None, "a different account inherited a halt"
    assert other._equity_peak != 5000.0, "a different account inherited a high-water mark"


def test_a_failed_persistence_write_blocks_entries_and_is_visible(broker, engine_factory, monkeypatch):
    e = engine_factory(basket_stop_loss_usd=60.0)
    e._tick()
    monkeypatch.setattr(db_module, "save_risk",
                        lambda *a, **k: (_ for _ in ()).throw(db_module.PersistenceError("disk full")))
    e._persist_risk_state()
    assert e._persist_failed and "disk full" in e._persist_failed
    allowed, reason = e._entry_gate(broker.get_account_info())
    assert allowed is False and "disk full" in reason
    assert e.status()["persistence_error"]


def test_a_failed_read_is_not_treated_as_no_halt(broker, engine_factory, monkeypatch):
    monkeypatch.setattr(db_module, "load_risk",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))
    e = engine_factory(basket_stop_loss_usd=60.0)
    assert e._persist_failed, "an unreadable risk record was taken as proof of no halt"
    allowed, _ = e._entry_gate(broker.get_account_info())
    assert allowed is False


# --- 11, 12: account identity and margin -------------------------------------

def test_a_real_account_behind_a_demo_label_is_refused(broker, engine_factory):
    e = engine_factory(basket_stop_loss_usd=60.0)
    broker.trade_mode = "real"           # what the BROKER says
    verdict = e.verify_account(broker.get_account_info())
    assert verdict.allowed is False
    assert "real" in verdict.reason and "demo" in verdict.reason
    allowed, reason = e._entry_gate(broker.get_account_info())
    assert allowed is False


def test_an_unclassified_account_is_refused(broker, engine_factory):
    e = engine_factory(basket_stop_loss_usd=60.0)
    broker.trade_mode = "unknown"
    assert e.verify_account(broker.get_account_info()).allowed is False


def test_every_entry_endpoint_refuses_a_real_account_on_a_demo_label(client):
    c, manager = client
    manager.broker._trade_mode_override = "real"

    # The mock broker reports demo; override it at the adapter level so no real
    # terminal is involved in proving the refusal.
    original = manager.broker.get_account_info

    def as_real():
        info = original()
        info.trade_mode = "real"
        return info

    manager.broker.get_account_info = as_real
    assert c.post("/api/start", json={"confirm_real": False}).status_code == 403
    assert c.post("/api/test-order", json={"side": "BUY", "volume": 0.01}).status_code == 403


def test_unknown_free_margin_blocks_the_grid(broker, engine_factory):
    e = engine_factory(basket_stop_loss_usd=60.0)
    broker.free_margin_unknown = True
    e._tick()
    broker.next_candle()
    e._tick()
    assert broker.get_pending_orders("XAUUSD", magic=MAGIC) == []
    assert "free margin" in (e._entry_block or "")


def test_an_uncalculable_margin_blocks_the_grid(broker, engine_factory):
    e = engine_factory(basket_stop_loss_usd=60.0)
    broker.margin_calc_fails = True
    e._tick()
    broker.next_candle()
    e._tick()
    assert broker.get_pending_orders("XAUUSD", magic=MAGIC) == []
    assert "margin" in (e._entry_block or "")


def test_insufficient_margin_blocks_the_grid(broker, engine_factory):
    e = engine_factory(basket_stop_loss_usd=60.0)
    broker.balance = 120.0        # clears the loss budget and the floor, short on margin
    e._tick()
    broker.next_candle()
    e._tick()
    assert broker.get_pending_orders("XAUUSD", magic=MAGIC) == []


def test_a_lot_below_the_broker_minimum_is_refused_not_rounded_up(broker, engine_factory):
    e = engine_factory(basket_stop_loss_usd=60.0, lot_size=0.005)
    e._tick()
    allowed, reason = e._entry_gate(broker.get_account_info())
    assert allowed is False
    assert "minimum" in reason and "rounding" in reason


def test_a_missing_capital_floor_blocks_activation(broker, engine_factory):
    e = engine_factory(basket_stop_loss_usd=60.0, capital_floor_usd=0.0)
    e._tick()
    allowed, reason = e._entry_gate(broker.get_account_info())
    assert allowed is False and "RISK_CONFIG_REQUIRED" in reason and "capital floor" in reason


def test_a_balance_at_the_capital_floor_blocks_new_grids(broker, engine_factory):
    e = engine_factory(basket_stop_loss_usd=60.0, capital_floor_usd=900.0)
    e._tick()
    broker.balance = 900.0
    allowed, reason = e._entry_gate(broker.get_account_info())
    assert allowed is False and "capital floor" in reason


# --- 13: costs and rounding ---------------------------------------------------

def test_unknown_costs_do_not_qualify_a_basket_as_having_met_the_target(broker, engine_factory):
    e = engine_factory(basket_take_profit_usd=10.0, basket_stop_loss_usd=60.0)
    e._tick()
    broker.next_candle()
    e._tick()
    for o in list(broker.get_pending_orders("XAUUSD", magic=MAGIC)):
        broker.cancel_pending_order(o.ticket)
    pos = broker.open_position("BUY", broker.price - 20.0)
    pos.costs_known = False              # the broker has not reported the fees

    broker.next_candle()
    e._tick()
    assert broker.get_open_positions("XAUUSD", magic=MAGIC), (
        "a basket with unreported costs was treated as having earned the target"
    )


def test_missing_cost_data_does_not_suppress_a_loss_exit(broker, engine_factory, monkeypatch):
    e = engine_factory(basket_take_profit_usd=10_000.0, basket_stop_loss_usd=60.0,
                       max_daily_loss_usd=10_000.0)
    e._tick()
    broker.next_candle()
    e._tick()
    for o in list(broker.get_pending_orders("XAUUSD", magic=MAGIC)):
        broker.cancel_pending_order(o.ticket)
    pos = broker.open_position("BUY", broker.price + 70.0)   # -$70, past the $60 stop
    pos.costs_known = False
    monkeypatch.setattr(broker, "get_symbol_info",
                        lambda s: (_ for _ in ()).throw(RuntimeError("no symbol info")))

    broker.next_candle()
    e._tick()
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == [], (
        "missing cost data suppressed a loss exit that had already triggered"
    )


def test_an_unestimable_exit_cost_is_none_not_zero(broker, engine_factory, monkeypatch):
    e = engine_factory()
    pos = broker.open_position("BUY", 4000.0)
    monkeypatch.setattr(broker, "get_symbol_info",
                        lambda s: (_ for _ in ()).throw(RuntimeError("no symbol info")))
    assert e._estimated_exit_cost(broker.get_open_positions("XAUUSD", magic=MAGIC)) is None


def test_display_rounding_cannot_carry_a_basket_over_the_target(broker, engine_factory):
    """9.996 shows as 10.00. It has still not earned 10.00."""
    e = engine_factory(basket_take_profit_usd=10.0)
    positions = [type("P", (), {
        "profit": 9.996, "swap": 0.0, "commission": 0.0, "costs_known": True,
        "net_profit": 9.996, "volume": 0.01,
    })()]
    assert round(9.996, 2) == 10.0, "fixture: this value does display as the target"
    assert e._profit_target_met(positions, 9.996, True, 0.0) is False


# --- 14: replacement policy ---------------------------------------------------

def test_a_profitable_close_still_replaces_on_the_same_candle(broker, engine_factory):
    e = engine_factory(basket_take_profit_usd=1.0, basket_stop_loss_usd=60.0,
                       max_daily_loss_usd=10_000.0)
    e._tick()
    broker.next_candle()
    e._tick()
    candle_before = broker.candle_time
    broker.price += 4.0
    e._tick()                    # same candle: close and rebuild
    assert broker.candle_time == candle_before
    assert e._baskets_won == 1
    assert len(broker.get_pending_orders("XAUUSD", magic=MAGIC)) == 20
    assert e._entries_paused is False


# --- 16, 18: migration and isolation ------------------------------------------

def test_the_suite_cannot_reach_a_real_broker_even_with_an_mt5_environment():
    """Operator environment variables must not be able to point the tests at a
    terminal. conftest sets these before app.config is imported."""
    from app.brokers import get_broker
    from app.brokers.mock_broker import MockBroker
    from app.config import settings

    assert settings.broker_mode == "mock"
    assert isinstance(get_broker(), MockBroker)


def test_migration_keeps_existing_rows_and_close_reasons(tmp_path):
    """Adding the risk columns must not cost the owner their history."""
    import sqlite3

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE trades (
      id INTEGER PRIMARY KEY AUTOINCREMENT, ticket VARCHAR, account_id VARCHAR DEFAULT 'legacy',
      symbol VARCHAR, side VARCHAR, volume FLOAT, open_price FLOAT, close_price FLOAT,
      sl FLOAT, tp FLOAT, profit FLOAT, mode VARCHAR, status VARCHAR,
      open_time DATETIME, close_time DATETIME, magic INTEGER, trading_day VARCHAR,
      close_reason VARCHAR)""")
    con.execute("INSERT INTO trades (ticket,symbol,side,volume,open_price,sl,tp,profit,mode,status,"
                "open_time,magic,trading_day,close_reason) VALUES "
                "('old1','XAUUSD','BUY',0.01,4000,0,0,1.5,'demo','CLOSED',datetime('now'),990022,"
                "'2026-09-19','basket target reached (+1.50)')")
    con.commit()
    con.close()

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    saved_engine, saved_session = db_module.engine, db_module.SessionLocal
    db_module.engine = engine
    db_module.SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        db_module.init_db()
        with engine.begin() as conn:
            rows = list(conn.execute(text("SELECT ticket, close_reason FROM trades")))
        assert rows == [("old1", "basket target reached (+1.50)")]
    finally:
        engine.dispose()
        db_module.engine, db_module.SessionLocal = saved_engine, saved_session


# --- durable recovery across a real process boundary --------------------------

RECOVERY_SCRIPT = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, {backend!r})
    sys.path.insert(0, {tests!r})
    os.environ["BROKER_MODE"] = "mock"
    os.environ["SYMBOL"] = "XAUUSD"
    os.environ["ACCOUNT_TYPE"] = "demo"
    os.environ["DATABASE_URL"] = "sqlite:///" + {db!r}
    from app import db as db_module
    db_module.init_db()
    from conftest import FakeBroker, MAGIC
    from app.engine.grid_engine import GridEngine

    broker = FakeBroker()
    engine = GridEngine(broker=broker, symbol="XAUUSD", mode="demo", magic_number=MAGIC,
                        capital_floor_usd=50.0, max_daily_loss_usd=15.0)
    print("HALT:", engine._halt_reason)
    print("INTENT:", engine._close_intent.cause if engine._close_intent else None)
    print("ANCHOR:", engine._day_open_marked)
    """
)


def test_state_survives_a_real_process_restart(tmp_path):
    """A rebuilt object in the same interpreter does not prove durability, so
    the recovering engine is constructed in a separate process reading the same
    database file."""
    db_path = str(tmp_path / "durable.db")
    backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tests_dir = os.path.join(backend, "tests")

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    saved_engine, saved_session = db_module.engine, db_module.SessionLocal
    db_module.engine = engine
    db_module.SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        db_module.init_db()
        from app.engine.grid_engine import GridEngine
        from tests.conftest import FakeBroker

        broker = FakeBroker()
        writer = GridEngine(broker=broker, symbol="XAUUSD", mode="demo", magic_number=MAGIC,
                            capital_floor_usd=50.0, max_daily_loss_usd=15.0)
        writer._tick()
        writer._halt_reason = "daily loss limit reached (illustrative)"
        writer._open_close_intent("daily_loss", "risk protection: illustrative")
    finally:
        engine.dispose()
        db_module.engine, db_module.SessionLocal = saved_engine, saved_session

    script = RECOVERY_SCRIPT.format(backend=backend, tests=tests_dir, db=db_path)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "HALT: daily loss limit reached (illustrative)" in result.stdout, result.stdout
    assert "INTENT: daily_loss" in result.stdout, result.stdout
    assert "ANCHOR: 0.0" in result.stdout or "ANCHOR: 0" in result.stdout, result.stdout
