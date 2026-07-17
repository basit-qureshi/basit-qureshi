from dataclasses import dataclass
from enum import Enum

import pandas as pd

from app.strategy.indicators import ema, rsi


class Signal(str, Enum):
    NONE = "NONE"
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class StrategyResult:
    signal: Signal
    reason: str
    ema_fast: float
    ema_slow: float
    rsi: float
    close: float


class EmaRsiStrategy:
    """EMA crossover for trend direction, filtered by RSI to avoid buying into
    overbought / selling into oversold conditions. Simple, explainable, backtestable
    starting point — an ML signal model can later replace/augment this class
    without touching the risk manager or trading engine.
    """

    def __init__(self, ema_fast: int = 12, ema_slow: int = 26, rsi_period: int = 14, rsi_upper: float = 70, rsi_lower: float = 30):
        self.ema_fast_period = ema_fast
        self.ema_slow_period = ema_slow
        self.rsi_period = rsi_period
        self.rsi_upper = rsi_upper
        self.rsi_lower = rsi_lower

    def generate_signal(self, candles: pd.DataFrame) -> StrategyResult:
        close = candles["close"]
        min_len = max(self.ema_slow_period, self.rsi_period) + 2
        if len(close) < min_len:
            return StrategyResult(Signal.NONE, "not enough candles", 0, 0, 0, float(close.iloc[-1]))

        ema_fast = ema(close, self.ema_fast_period)
        ema_slow = ema(close, self.ema_slow_period)
        rsi_series = rsi(close, self.rsi_period)

        fast_now, fast_prev = ema_fast.iloc[-1], ema_fast.iloc[-2]
        slow_now, slow_prev = ema_slow.iloc[-1], ema_slow.iloc[-2]
        rsi_now = rsi_series.iloc[-1]
        close_now = close.iloc[-1]

        crossed_up = fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev >= slow_prev and fast_now < slow_now

        if crossed_up and rsi_now < self.rsi_upper:
            return StrategyResult(Signal.BUY, "EMA bullish cross + RSI not overbought", fast_now, slow_now, rsi_now, close_now)
        if crossed_down and rsi_now > self.rsi_lower:
            return StrategyResult(Signal.SELL, "EMA bearish cross + RSI not oversold", fast_now, slow_now, rsi_now, close_now)

        return StrategyResult(Signal.NONE, "no crossover", fast_now, slow_now, rsi_now, close_now)
