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
