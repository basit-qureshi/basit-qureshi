import json
from datetime import datetime, timedelta, timezone
import pytest
from app import db
from app.brokers.base import PendingType
from app.db import TradeRecord
from tests.conftest import MAGIC


def activate(broker, engine_factory, **kwargs):
    engine = engine_factory(**kwargs)
    engine._tick()
    broker.next_candle()
    engine._tick()
    return engine


def test_failed_cancel_blocks_new_grid_until_the_old_order_is_gone(broker, engine_factory, monkeypatch):
    e = activate(broker, engine_factory)
    old = set(broker.pending)
    stubborn = next(o.ticket for o in broker.pending.values() if o.order_type == PendingType.SELL_STOP)
    cancel = broker.cancel_pending_order
    def reject_one(ticket):
        if ticket == stubborn:
            raise RuntimeError("temporary rejection")
        cancel(ticket)
    monkeypatch.setattr(broker, "cancel_pending_order", reject_one)
    broker.price = 4004
    e._tick()
    assert e._baskets_won == 0 and set(broker.pending) <= old
    assert e._profit_exit_reason
    monkeypatch.setattr(broker, "cancel_pending_order", cancel)
    e._tick()
    assert len(broker.pending) == 20 and not (set(broker.pending) & old)
    assert e._baskets_won == 1


def test_failed_close_retries_even_after_profit_falls(broker, engine_factory, monkeypatch):
    e = activate(broker, engine_factory)
    close = broker.close_position
    def reject(ticket):
        raise RuntimeError("retry later")
    monkeypatch.setattr(broker, "close_position", reject)
    broker.price = 4004
    e._tick()
    assert broker.positions and not broker.pending and e._baskets_won == 0
    broker.price = 4002
    monkeypatch.setattr(broker, "close_position", close)
    e._tick()
    assert not broker.positions and len(broker.pending) == 20
    assert e._baskets_won == 1


def test_pending_settlement_resumes_without_waiting_for_another_candle(broker, engine_factory, monkeypatch):
    e = activate(broker, engine_factory)
    settled = broker.get_realized_profit
    monkeypatch.setattr(broker, "get_realized_profit", lambda ticket: None)
    broker.price = 4004
    stamp = broker.candle_time
    e._tick()
    assert not broker.pending and e._profit_restart_pending
    monkeypatch.setattr(broker, "get_realized_profit", settled)
    e._tick()
    assert broker.candle_time == stamp and len(broker.pending) == 20


def test_daily_target_still_blocks_immediate_rebuild(broker, engine_factory):
    e = activate(broker, engine_factory, daily_profit_target_usd=10)
    broker.price = 4004
    e._tick()
    assert e._daily_target_hit
    assert not broker.pending and not broker.positions


def test_loss_exit_still_waits_for_next_candle(broker, engine_factory):
    # A $2 stop cannot accommodate a 10+10 grid, which freezes around -$37.80
    # once both sides fill, so the budget is raised and the seeded loss with it.
    # What this test checks is the candle gate after a loss exit, not the size.
    broker.open_position("BUY", 4070)
    e = engine_factory(basket_stop_loss_usd=60)
    e._tick()
    assert not broker.positions and not broker.pending
    e._tick()
    assert not broker.pending
    broker.next_candle()
    e._tick()
    assert len(broker.pending) == 20


def test_original_grid_has_fixed_lots_spacing_and_no_new_entry_filters(broker, engine_factory, monkeypatch):
    def unwanted(*args):
        raise AssertionError("Restored strategy must not invoke the added margin gate")
    monkeypatch.setattr(broker, "check_grid_margin", unwanted, raising=False)
    broker.spread = .9
    e = activate(broker, engine_factory)
    buys = sorted(o.price for o in broker.pending.values() if o.order_type == PendingType.BUY_STOP)
    assert e.grid_distance == .30 and e.poll_interval_seconds == 5
    assert len(broker.pending) == 20
    assert all(o.volume == .01 for o in broker.pending.values())
    assert all(abs(b-a-.30) < 1e-8 for a, b in zip(buys, buys[1:]))


def test_grid_restore_is_once_and_keeps_saved_risk_settings(tmp_path, monkeypatch):
    import app.bot_manager as module
    path = tmp_path / "runtime_settings.json"
    saved = {"symbol": "XAUUSDm", "grid_distance": .8, "grid_lot_size": .03,
             "grid_buy_stop_levels": 3, "grid_sell_stop_levels": 4,
             "grid_basket_take_profit_usd": 20, "grid_max_daily_loss_usd": 17,
             "poll_interval_seconds": 30, "grid_daily_profit_target_usd": 7}
    path.write_text(json.dumps(saved))
    monkeypatch.setattr(module, "_SETTINGS_FILE", path)
    manager = module.BotManager()
    assert manager.settings["grid_distance"] == .30
    assert manager.settings["grid_lot_size"] == .01
    assert manager.settings["grid_buy_stop_levels"] == manager.settings["grid_sell_stop_levels"] == 10
    assert manager.settings["grid_basket_take_profit_usd"] == 10
    assert manager.settings["symbol"] == "XAUUSDm"
    assert manager.settings["poll_interval_seconds"] == 30
    assert manager.settings["grid_max_daily_loss_usd"] == 17
    assert manager.settings["grid_daily_profit_target_usd"] == 7
    assert json.loads(path.with_name("runtime_settings.before_grid_restore.json").read_text()) == saved
    manager.update_settings({"grid_distance": .40})
    assert module.BotManager().settings["grid_distance"] == .40


def test_database_utc_timestamps_are_explicit_on_the_api():
    from app.api.routes import utc_iso
    naive = datetime(2026, 9, 14, 16, 53, 20)
    pakistan = datetime(2026, 9, 14, 21, 53, 20, tzinfo=timezone(timedelta(hours=5)))
    assert utc_iso(naive) == utc_iso(pakistan) == "2026-09-14T16:53:20+00:00"
    assert utc_iso(None) is None


def test_existing_account_ledger_remains_compatible_on_restart(broker, engine_factory):
    broker.open_position("BUY", 4000)
    for _ in range(2):
        engine_factory()._tick()
    with db.SessionLocal() as session:
        assert session.query(TradeRecord).filter(TradeRecord.status != "DUPLICATE").count() == 1


def test_mt5_restores_original_spacing_buffer_and_no_individual_sl_tp():
    import threading
    from types import SimpleNamespace
    from app.brokers.mt5_broker import MT5Broker
    captured = []
    info = SimpleNamespace(visible=True, point=.001, digits=3, trade_tick_value=.1,
        trade_tick_size=.001, volume_min=.01, volume_step=.01, trade_stops_level=0)
    broker = MT5Broker.__new__(MT5Broker)
    broker._lock = threading.RLock()
    def send(request):
        captured.append(request)
        return SimpleNamespace(retcode=8, order=123)
    broker._mt5 = SimpleNamespace(symbol_info=lambda symbol: info,
        symbol_info_tick=lambda symbol: SimpleNamespace(bid=4000, ask=4000.24),
        order_send=send, ORDER_TYPE_BUY_STOP=1, ORDER_TYPE_SELL_STOP=2,
        TRADE_ACTION_PENDING=3, ORDER_TIME_GTC=4, ORDER_FILLING_RETURN=5,
        TRADE_RETCODE_DONE=9, TRADE_RETCODE_PLACED=8)
    broker.place_pending_order("XAUUSDm", PendingType.BUY_STOP, .01, 4000.3)
    assert "sl" not in captured[0] and "tp" not in captured[0]
    assert captured[0]["price"] == 4000.3
    assert broker.get_symbol_info("XAUUSDm").min_stop_distance == pytest.approx(.72)


def test_daily_target_rollover_keeps_the_original_new_day_gate(broker, engine_factory):
    e = activate(broker, engine_factory, daily_profit_target_usd=10)
    broker.price = 4004
    e._tick()
    assert e._daily_target_hit and not broker.pending
    broker.next_candle(minutes=24*60)
    e._tick()
    assert not broker.pending
    broker.next_candle()
    e._tick()
    assert len(broker.pending) == 20
