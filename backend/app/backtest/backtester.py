import pandas as pd
import yfinance as yf

from app.brokers.base import SymbolInfo
from app.risk.risk_manager import RiskManager
from app.strategy.ema_rsi_strategy import EmaRsiStrategy, Signal

_YF_SYMBOL_MAP = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X",
    "USDCHF": "USDCHF=X",
    "USDCAD": "USDCAD=X",
    "XAUUSD": "GC=F",  # Gold futures continuous contract — closest free proxy for spot gold
}

# Local pip-size table so the backtester has no dependency on a live broker connection.
_PIP_SIZES = {
    "EURUSD": 0.0001,
    "GBPUSD": 0.0001,
    "USDJPY": 0.01,
    "AUDUSD": 0.0001,
    "USDCHF": 0.0001,
    "USDCAD": 0.0001,
    "XAUUSD": 0.01,
}


def fetch_history(symbol: str, period: str = "60d", interval: str = "15m") -> pd.DataFrame:
    yf_symbol = _YF_SYMBOL_MAP.get(symbol, f"{symbol}=X")
    df = yf.download(yf_symbol, period=period, interval=interval, progress=False, auto_adjust=True)
    if df.empty:
        raise ValueError(f"No historical data returned for {symbol} ({yf_symbol}). Try a shorter period/interval.")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)
    df.index.name = "time"
    return df[["open", "high", "low", "close", "volume"]]


def run_backtest(
    symbol: str,
    strategy: EmaRsiStrategy,
    risk_manager: RiskManager,
    starting_balance: float = 10_000.0,
    period: str = "60d",
    interval: str = "15m",
) -> dict:
    df = fetch_history(symbol, period, interval)
    pip_size = _PIP_SIZES.get(symbol, 0.0001)
    symbol_info = SymbolInfo(
        symbol=symbol, pip_size=pip_size, pip_value_per_lot=10.0, min_volume=0.01, volume_step=0.01
    )

    balance = starting_balance
    equity_curve: list[dict] = []
    trades: list[dict] = []
    open_trade: dict | None = None

    min_len = max(strategy.ema_slow_period, strategy.rsi_period) + 2

    for i in range(min_len, len(df)):
        window = df.iloc[: i + 1]
        bar = df.iloc[i]

        if open_trade:
            hit_sl = (open_trade["side"] == "BUY" and bar["low"] <= open_trade["sl"]) or (
                open_trade["side"] == "SELL" and bar["high"] >= open_trade["sl"]
            )
            hit_tp = (open_trade["side"] == "BUY" and bar["high"] >= open_trade["tp"]) or (
                open_trade["side"] == "SELL" and bar["low"] <= open_trade["tp"]
            )
            if hit_sl or hit_tp:
                exit_price = open_trade["sl"] if hit_sl else open_trade["tp"]
                pips = (exit_price - open_trade["entry"]) / pip_size
                if open_trade["side"] == "SELL":
                    pips = -pips
                profit = round(pips * open_trade["volume"] * 10.0, 2)
                balance = round(balance + profit, 2)
                trades.append(
                    {
                        "side": open_trade["side"],
                        "volume": open_trade["volume"],
                        "entry": round(open_trade["entry"], 5),
                        "exit": round(float(exit_price), 5),
                        "sl": round(open_trade["sl"], 5),
                        "tp": round(open_trade["tp"], 5),
                        "profit": profit,
                        "result": "TP" if hit_tp else "SL",
                        "open_time": str(df.index[open_trade["open_index"]]),
                        "close_time": str(df.index[i]),
                    }
                )
                open_trade = None

        if not open_trade:
            result = strategy.generate_signal(window)
            if result.signal != Signal.NONE:
                decision = risk_manager.evaluate(balance, balance, 0, symbol_info)
                if decision.allowed:
                    side = result.signal.value
                    entry = float(bar["close"])
                    sl, tp = risk_manager.compute_sl_tp(entry, side, pip_size)
                    open_trade = {
                        "side": side,
                        "volume": decision.volume,
                        "entry": entry,
                        "sl": float(sl),
                        "tp": float(tp),
                        "open_index": i,
                    }

        equity_curve.append({"time": str(df.index[i]), "equity": balance})

    wins = [t for t in trades if t["profit"] > 0]
    losses = [t for t in trades if t["profit"] <= 0]
    total_profit = round(sum(t["profit"] for t in trades), 2)
    gross_profit = sum(t["profit"] for t in wins)
    gross_loss = abs(sum(t["profit"] for t in losses))

    peak = starting_balance
    max_drawdown = 0.0
    for point in equity_curve:
        peak = max(peak, point["equity"])
        drawdown = (peak - point["equity"]) / peak * 100 if peak else 0
        max_drawdown = max(max_drawdown, drawdown)

    return {
        "symbol": symbol,
        "period": period,
        "interval": interval,
        "starting_balance": starting_balance,
        "ending_balance": balance,
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 2) if trades else 0,
        "total_profit": total_profit,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
        "max_drawdown_percent": round(max_drawdown, 2),
        "equity_curve": equity_curve,
        "trades": trades,
    }
