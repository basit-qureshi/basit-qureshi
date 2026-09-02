"""The backtester must model the same rules the live engine follows."""

import numpy as np
import pandas as pd

from app.backtest.backtester import run_grid_backtest


def candles(n=3000, seed=5, start=4000.0, freq="1min", step=0.35):
    rng = np.random.default_rng(seed)
    close = start + np.cumsum(rng.normal(0, step, n))
    open_ = np.concatenate([[start], close[:-1]])
    high = np.maximum(close + np.abs(rng.normal(0, step / 2, n)), np.maximum(open_, close))
    low = np.minimum(close - np.abs(rng.normal(0, step / 2, n)), np.minimum(open_, close))
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": 100.0},
        index=pd.date_range("2026-01-05", periods=n, freq=freq),
    )


def run(df, **kw):
    kw.setdefault("starting_balance", 1000.0)
    kw.setdefault("spread_points", 24.0)
    kw.setdefault("max_daily_loss_usd", 0)
    kw.setdefault("max_equity_drawdown_percent", 0)
    return run_grid_backtest(symbol="XAUUSD", df=df, **kw)


# 18 ----------------------------------------------------------------------
def test_no_basket_starts_on_the_activation_bar():
    df = candles()
    r = run(df, basket_stop_loss_usd=20)
    first_bar = str(df.index[0])
    assert all(t["open_time"] != first_bar for t in r["trades"]), "a basket started on the activation bar"


def test_no_basket_starts_on_the_bar_another_closed_on():
    r = run(candles(), basket_stop_loss_usd=20)
    closes = {t["close_time"] for t in r["trades"]}
    opens = [t["open_time"] for t in r["trades"]]
    assert not (closes & set(opens)), "a basket opened on a bar another closed on"
    assert len(opens) == len(set(opens)), "two baskets opened on the same bar"


# backtest daily target ---------------------------------------------------
def test_daily_target_halts_and_is_reported():
    df = candles(n=4000)
    without = run(df, basket_stop_loss_usd=20, daily_profit_target_usd=0)
    with_target = run(df, basket_stop_loss_usd=20, daily_profit_target_usd=10.0)
    assert without["daily_target_halts"] == 0
    assert with_target["daily_target_halts"] >= 1
    assert with_target["total_trades"] < without["total_trades"], "the target did not stop any trading"


def test_daily_target_resets_on_the_next_day():
    # two full days of one-minute candles
    df = candles(n=60 * 24 * 2 + 100)
    r = run(df, basket_stop_loss_usd=20, daily_profit_target_usd=10.0)
    days = {t["close_time"][:10] for t in r["trades"]}
    assert len(days) > 1, "trading never resumed on the second day"
    assert r["daily_target_halts"] >= 1


def test_target_of_zero_disables_the_daily_halt():
    r = run(candles(), basket_stop_loss_usd=20, daily_profit_target_usd=0)
    assert r["daily_target_halts"] == 0


def test_existing_result_fields_survive():
    r = run(candles(), basket_stop_loss_usd=20)
    for key in (
        "total_trades", "win_rate", "total_profit", "profit_factor", "max_drawdown_percent",
        "worst_trade", "best_trade", "max_positions_open", "equity_curve", "trades",
    ):
        assert key in r
