import json
import numpy as np
import pandas as pd
import pytest
from app import db
from app.ai.gate import AIGate, features
from app.ai.train import train
from app.backtest.backtester import run_grid_backtest
from app.brokers.base import AccountInfo
from app.db import TradeRecord


def test_restart_does_not_duplicate_position(broker, engine_factory):
    broker.open_position("BUY", 4000)
    first = engine_factory()
    first._tick()
    second = engine_factory()
    second._tick()
    with db.SessionLocal() as session:
        assert session.query(TradeRecord).count() == 1


def test_account_mode_mismatch_blocks_orders(broker, engine_factory, monkeypatch):
    monkeypatch.setattr(broker, "get_account_info", lambda: AccountInfo(1000, 1000, "USD", 100, trade_mode="real"))
    with pytest.raises(RuntimeError, match="account"):
        engine_factory()._tick()
    assert not broker.pending


def test_daily_loss_survives_engine_restart(broker, engine_factory):
    with db.SessionLocal() as session:
        session.add(TradeRecord(ticket="loss", symbol="XAUUSD", side="BUY", volume=.01,
                    open_price=4000, sl=0, tp=0, status="CLOSED", profit=-20, mode="demo",
                    magic=990022, trading_day="2026-01-05"))
        session.commit()
    for _ in range(2):
        e = engine_factory(max_daily_loss_usd=10)
        e._tick()
        broker.next_candle()
        e._tick()
        assert e._halt_reason
        assert not broker.pending


def test_failed_close_is_retried_after_profit_disappears(broker, engine_factory, monkeypatch):
    broker.open_position("BUY", 3980)
    e = engine_factory(max_daily_loss_usd=0, max_equity_drawdown_percent=0)
    real_close = broker.close_position
    def fail(ticket):
        raise RuntimeError("temporary broker rejection")
    monkeypatch.setattr(broker, "close_position", fail)
    e._tick()
    assert e._closing_reason and broker.positions
    broker.price = 3980
    monkeypatch.setattr(broker, "close_position", real_close)
    e._tick()
    assert not broker.positions and not e._closing_reason


def test_unsettled_result_is_retried_without_restart(broker, engine_factory, monkeypatch):
    broker.open_position("BUY", 3980)
    e = engine_factory()
    original = broker.get_realized_profit
    monkeypatch.setattr(broker, "get_realized_profit", lambda ticket: None)
    e._tick()
    assert e.daily_summary()["today_unsettled_trades"] == 1
    monkeypatch.setattr(broker, "get_realized_profit", original)
    e._tick()
    assert e.daily_summary()["today_unsettled_trades"] == 0


def test_costs_are_included_in_target(broker, engine_factory, monkeypatch):
    pos = broker.open_position("BUY", 3990)
    original = broker.get_open_positions
    def positions(*args, **kwargs):
        out = original(*args, **kwargs)
        for p in out:
            p.swap = -1
        return out
    monkeypatch.setattr(broker, "get_open_positions", positions)
    e = engine_factory()
    e._tick()
    assert pos.ticket in broker.positions


def test_no_future_tenth_fill_at_an_earlier_target():
    frame = pd.DataFrame(
        {"open": [4000, 4000], "high": [4000, 4004], "low": [4000, 4000],
         "close": [4000, 4004], "volume": [1, 1]},
        index=pd.date_range("2026-01-05", periods=2, freq="1min"))
    out = run_grid_backtest(df=frame, max_daily_loss_usd=0, max_equity_drawdown_percent=0)
    assert out["trades"][0]["positions"] == 9
    assert out["trades"][0]["profit"] == 10


def test_missing_model_blocks_filter_but_shadow_reports_it(tmp_path, broker):
    gate = AIGate(tmp_path / "absent.json", "filter")
    assert gate.allow(broker, "XAUUSD") is False
    assert "unavailable" in gate.reason
    assert AIGate(tmp_path / "absent.json", "shadow").allow(broker, "XAUUSD")


def test_features_do_not_change_when_future_prices_change():
    rng = np.random.default_rng(7)
    close = 4000 + rng.normal(0, 1, 200).cumsum()
    data = pd.DataFrame({"close": close, "high": close+1, "low": close-1})
    before = features(data).iloc[:100]
    data.loc[100:, "close"] *= 2
    pd.testing.assert_frame_equal(before, features(data).iloc[:100])


def test_train_artifact_has_purged_holdout_and_finite_weights():
    rng = np.random.default_rng(42)
    close = 4000 + rng.normal(0, .5, 3200).cumsum()
    data = pd.DataFrame({"close": close, "high": close+1, "low": close-1},
                        index=pd.date_range("2026-01-05", periods=3200, freq="1min", tz="UTC"))
    model = train(data, "XAUUSDm")
    assert model["evaluation"]["purge_rows"] == 5
    assert model["evaluation"]["holdout_rows"] > 500
    assert np.isfinite(model["weights"]).all()
    json.dumps(model, allow_nan=False)


def test_mt5_error_is_not_an_empty_position_list():
    import threading
    from types import SimpleNamespace
    from app.brokers.mt5_broker import MT5Broker
    broker = MT5Broker.__new__(MT5Broker)
    broker._lock = threading.RLock()
    broker._mt5 = SimpleNamespace(positions_get=lambda **kw: None, last_error=lambda: (1, "failed"))
    with pytest.raises(RuntimeError, match="unavailable"):
        broker.get_open_positions()


def test_tick_replay_leaves_live_database_untouched():
    from app.tools.replay_ticks import replay
    original = db.engine
    ticks = pd.DataFrame({
        "time": pd.date_range("2026-01-05", periods=150, freq="s", tz="UTC"),
        "bid": np.linspace(4000, 4006, 150), "ask": np.linspace(4000.24, 4006.24, 150)})
    report = replay(ticks, {"symbol": "XAUUSDm", "max_daily_loss_usd": 0,
                            "max_equity_drawdown_percent": 0}, 1000)
    assert db.engine is original
    assert report["max_positions"] <= 20
    assert np.isfinite(report["ending_equity"])


def test_csv_without_magic_is_not_claimed_as_bot_performance(tmp_path):
    from app.tools.audit_report import analyze
    path = tmp_path / "history.csv"
    path.write_text("Profit,Swap,Commission\n10,0,-1\n-4,0,-1\n")
    result = analyze(path)
    assert set(result["groups"]) == {"unclassified"}
    assert result["groups"]["unclassified"]["net"] == 4



def test_position_identifier_survives_ticket_change(broker, engine_factory):
    position = broker.open_position("BUY", 4000)
    position.identifier = "permanent-id"
    e = engine_factory()
    e._record_new_fills([position])
    position.ticket = "replacement-ticket"
    e._record_new_fills([position])
    with db.SessionLocal() as session:
        rows = session.query(TradeRecord).all()
        assert len(rows) == 1
        assert rows[0].ticket == "permanent-id"


def test_broker_history_restores_losses_without_reassigning_legacy(broker, engine_factory, monkeypatch):
    with db.SessionLocal() as session:
        session.add(TradeRecord(ticket="legacy", symbol="XAUUSD", side="BUY", volume=.01,
                    open_price=4000, sl=0, tp=0, status="CLOSED", profit=100, mode="demo",
                    magic=990022, trading_day="2026-01-05"))
        session.commit()
    monkeypatch.setattr(broker, "get_account_info",
        lambda: AccountInfo(1000, 1000, "USD", 100, account_id="mt5:test-account"))
    monkeypatch.setattr(broker, "history_records", lambda symbol, magic: [{
        "ticket": "closed-while-offline", "symbol": symbol, "side": "BUY", "volume": .01,
        "open_price": 4000, "open_time": broker.candle_time,
        "close_time": broker.candle_time, "profit": -20,
    }], raising=False)
    e = engine_factory(max_daily_loss_usd=10)
    e._tick()
    assert e._halt_reason and e.daily_net() == -20
    with db.SessionLocal() as session:
        assert session.query(TradeRecord).filter_by(ticket="legacy").one().account_id == "legacy"
        assert session.query(TradeRecord).filter_by(account_id="mt5:test-account").count() == 1


def test_history_outage_does_not_block_basket_stop(broker, engine_factory, monkeypatch):
    broker.open_position("BUY", 4020)
    def unavailable(*args):
        raise RuntimeError("history is temporarily unavailable")
    monkeypatch.setattr(broker, "history_records", unavailable, raising=False)
    monkeypatch.setattr(broker, "get_realized_profit", unavailable)
    e = engine_factory(basket_stop_loss_usd=10, max_daily_loss_usd=0, max_equity_drawdown_percent=0)
    e._tick()
    assert not broker.positions
    assert not e._closing_reason


def test_failed_close_is_not_reported_as_a_won_basket(broker, engine_factory, monkeypatch):
    broker.open_position("BUY", 3980)
    def unavailable(ticket):
        raise RuntimeError("rejected")
    monkeypatch.setattr(broker, "close_position", unavailable)
    e = engine_factory()
    messages = []
    e.on_update = messages.append
    e._tick()
    assert e._baskets_won == 0
    assert messages[-1]["grid"]["open_positions"] == 1


def test_mt5_pending_stop_is_aligned_to_tick_and_accepts_placed():
    import threading
    from types import SimpleNamespace
    from app.brokers.base import PendingType
    from app.brokers.mt5_broker import MT5Broker
    captured = []
    symbol = SimpleNamespace(visible=True, trade_tick_size=.25, point=.01, digits=2,
        volume_min=.01, volume_max=2, volume_step=.01, trade_stops_level=1)
    broker = MT5Broker.__new__(MT5Broker)
    broker._lock = threading.RLock()
    def send(request):
        captured.append(request)
        return SimpleNamespace(retcode=8, order=123)
    broker._mt5 = SimpleNamespace(symbol_info=lambda name: symbol, order_send=send,
        ORDER_TYPE_BUY_STOP=1, ORDER_TYPE_SELL_STOP=2, TRADE_ACTION_PENDING=3,
        ORDER_TIME_GTC=4, ORDER_FILLING_RETURN=5, TRADE_RETCODE_DONE=9, TRADE_RETCODE_PLACED=8)
    order = broker.place_pending_order("XAUUSDm", PendingType.BUY_STOP, .01, 4000.12)
    assert order.ticket == "123"
    assert captured[0]["price"] == 4000.25 and captured[0]["sl"] == 3995.25
    with pytest.raises(ValueError, match="volume"):
        broker.place_pending_order("XAUUSDm", PendingType.BUY_STOP, .015, 4000.12)


def test_ai_availability_includes_the_last_future_label():
    rng = np.random.default_rng(42)
    close = 4000 + rng.normal(0, .5, 3200).cumsum()
    frame = pd.DataFrame({"close": close, "high": close+1, "low": close-1},
        index=pd.date_range("2026-01-05", periods=3200, freq="1min", tz="UTC"))
    model = train(frame, "XAUUSDm")
    assert pd.Timestamp(model["training_end"]) > frame.index[-1]


def test_csv_decimal_comma_is_rejected_instead_of_multiplied(tmp_path):
    from app.tools.audit_report import analyze
    path = tmp_path / "history.csv"
    path.write_text('Profit;Swap;Commission\n"1,25";0;0\n')
    with pytest.raises(ValueError, match="Decimal-comma"):
        analyze(path)


def test_replay_stop_fills_between_polls_are_recorded():
    from app.tools.replay_ticks import ReplayBroker
    from app.brokers.base import PendingType
    from types import SimpleNamespace
    broker = ReplayBroker("XAUUSDm", 1000, 100)
    broker.connect()
    stamp = pd.Timestamp("2026-01-05T09:00:00Z")
    broker.step(SimpleNamespace(time=stamp, bid=4000, ask=4000.24))
    broker.place_pending_order("XAUUSDm", PendingType.BUY_STOP, .01, 4000.30, magic=990022)
    broker.step(SimpleNamespace(time=stamp+pd.Timedelta(seconds=1), bid=4000.20, ask=4000.44))
    broker.step(SimpleNamespace(time=stamp+pd.Timedelta(seconds=2), bid=3994, ask=3994.24))
    assert not broker._positions
    history = broker.history_records("XAUUSDm", 990022)
    assert len(history) == 1 and history[0]["profit"] == -6.44
