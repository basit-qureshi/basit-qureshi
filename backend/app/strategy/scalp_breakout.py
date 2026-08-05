import pandas as pd

from app.strategy.ema_rsi_strategy import Signal, StrategyResult
from app.strategy.indicators import ema, rsi


class ScalpBreakoutStrategy:
    """Momentum scalping for fast timeframes (M1/M5).

    Unlike the pure crossover strategy (which only fires at the moment two EMAs
    cross), this one can enter at any point while a trend is in place:

    - Direction comes from the EMA trend (fast above slow = only BUYs,
      fast below slow = only SELLs) — so it trades both directions as the
      market turns, not just whichever way the last crossover pointed.
    - Entry triggers on a breakout: the current close exceeding the high
      (or low) of the last `breakout_lookback` candles.
    - Volume must be above average (`volume_factor` x the 20-candle mean) so
      thin, drifting markets don't trigger entries that immediately stall.
    - RSI guards against buying a spike top / selling a spike bottom.
    """

    def __init__(
        self,
        ema_fast: int = 5,
        ema_slow: int = 13,
        rsi_period: int = 14,
        breakout_lookback: int = 6,
        volume_factor: float = 1.2,
    ):
        self.ema_fast_period = ema_fast
        self.ema_slow_period = ema_slow
        self.rsi_period = rsi_period
        self.breakout_lookback = breakout_lookback
        self.volume_factor = volume_factor

    def generate_signal(self, candles: pd.DataFrame) -> StrategyResult:
        close = candles["close"]
        min_len = max(self.ema_slow_period, self.rsi_period, self.breakout_lookback + 1, 21) + 1
        if len(close) < min_len:
            return StrategyResult(Signal.NONE, "not enough candles", 0, 0, 0, float(close.iloc[-1]))

        ema_fast = ema(close, self.ema_fast_period)
        ema_slow = ema(close, self.ema_slow_period)
        rsi_series = rsi(close, self.rsi_period)

        fast_now = float(ema_fast.iloc[-1])
        slow_now = float(ema_slow.iloc[-1])
        rsi_now = float(rsi_series.iloc[-1])
        close_now = float(close.iloc[-1])

        volume = candles["volume"]
        avg_volume = float(volume.iloc[-21:-1].mean())
        volume_ok = avg_volume <= 0 or float(volume.iloc[-1]) >= avg_volume * self.volume_factor

        recent = candles.iloc[-(self.breakout_lookback + 1) : -1]
        broke_up = close_now > float(recent["high"].max())
        broke_down = close_now < float(recent["low"].min())

        if not volume_ok:
            return StrategyResult(
                Signal.NONE, "volume below average — no momentum", fast_now, slow_now, rsi_now, close_now
            )

        # Breakout entries happen WITH momentum, where RSI is naturally elevated —
        # so the guard only blocks true blow-off extremes, unlike the crossover
        # strategy's tighter 70/30 bounds.
        if fast_now > slow_now and broke_up and rsi_now < 85:
            return StrategyResult(
                Signal.BUY, "uptrend + breakout above recent highs + volume", fast_now, slow_now, rsi_now, close_now
            )
        if fast_now < slow_now and broke_down and rsi_now > 15:
            return StrategyResult(
                Signal.SELL, "downtrend + breakdown below recent lows + volume", fast_now, slow_now, rsi_now, close_now
            )

        trend = "up" if fast_now > slow_now else "down"
        return StrategyResult(Signal.NONE, f"trend {trend}, waiting for breakout", fast_now, slow_now, rsi_now, close_now)
