import pandas as pd

from app.strategy.ema_rsi_strategy import Signal, StrategyResult
from app.strategy.indicators import ema


class MomentumScalpStrategy:
    """High-frequency scalping: enters on any strong push in the trend direction.

    Built for traders who want a steady stream of quick trades rather than a
    handful of high-conviction ones. It waits for only two things, both of
    which happen many times an hour on M1:

    - Direction: a short fast EMA above/below a short slow EMA.
    - Trigger: the last closed candle pushed past the previous candle's high
      (or low) and closed with a solid body — i.e. it closed near its extreme
      rather than wicking back, which is what separates a real push from
      chop.

    That is deliberately a much lower bar than the M5-breakout/M1-retest
    strategy. It trades far more often, and each individual signal is
    correspondingly weaker — frequency is bought with selectivity, not for
    free.
    """

    # body_ratio: how much of the candle's range must be body (not wick).
    # volume_factor: 0 disables the volume check entirely.
    SENSITIVITY_PRESETS = {
        "aggressive": dict(ema_fast=3, ema_slow=8, body_ratio=0.30, volume_factor=0.0),
        "balanced": dict(ema_fast=4, ema_slow=10, body_ratio=0.45, volume_factor=0.8),
        "conservative": dict(ema_fast=5, ema_slow=13, body_ratio=0.60, volume_factor=1.0),
    }

    @classmethod
    def from_sensitivity(cls, sensitivity: str) -> "MomentumScalpStrategy":
        preset = cls.SENSITIVITY_PRESETS.get(sensitivity, cls.SENSITIVITY_PRESETS["balanced"])
        return cls(**preset)

    def __init__(
        self,
        ema_fast: int = 4,
        ema_slow: int = 10,
        body_ratio: float = 0.45,
        volume_factor: float = 0.8,
    ):
        self.ema_fast_period = ema_fast
        self.ema_slow_period = ema_slow
        self.body_ratio = body_ratio
        self.volume_factor = volume_factor
        self.rsi_period = 14  # kept for the chart overlay / backtest sizing

    def generate_signal(self, candles: pd.DataFrame) -> StrategyResult:
        close = candles["close"]
        min_len = max(self.ema_slow_period, 21) + 3
        if len(close) < min_len:
            return StrategyResult(Signal.NONE, "not enough candles", 0, 0, 0, float(close.iloc[-1]))

        ema_fast = ema(close, self.ema_fast_period)
        ema_slow = ema(close, self.ema_slow_period)

        # Evaluate the last CLOSED candle — the forming one can still reverse.
        candle = candles.iloc[-2]
        prev = candles.iloc[-3]

        open_ = float(candle["open"])
        high = float(candle["high"])
        low = float(candle["low"])
        close_now = float(candle["close"])
        candle_range = high - low

        fast_now = float(ema_fast.iloc[-2])
        slow_now = float(ema_slow.iloc[-2])

        if candle_range <= 0:
            return StrategyResult(Signal.NONE, "flat candle", fast_now, slow_now, 0, close_now)

        body_strength = abs(close_now - open_) / candle_range
        strong_body = body_strength >= self.body_ratio

        avg_volume = float(candles["volume"].iloc[-21:-1].mean())
        volume_ok = (
            self.volume_factor <= 0 or avg_volume <= 0 or float(candle["volume"]) >= avg_volume * self.volume_factor
        )

        pushed_up = close_now > float(prev["high"])
        pushed_down = close_now < float(prev["low"])

        if fast_now > slow_now and pushed_up and strong_body and volume_ok:
            return StrategyResult(
                Signal.BUY, "uptrend + strong push above previous high", fast_now, slow_now, 0, close_now
            )
        if fast_now < slow_now and pushed_down and strong_body and volume_ok:
            return StrategyResult(
                Signal.SELL, "downtrend + strong push below previous low", fast_now, slow_now, 0, close_now
            )

        trend = "up" if fast_now > slow_now else "down"
        missing = []
        if not (pushed_up or pushed_down):
            missing.append("push past prev candle")
        if not strong_body:
            missing.append(f"strong body (was {body_strength:.0%}, need {self.body_ratio:.0%})")
        if not volume_ok:
            missing.append("volume")
        return StrategyResult(
            Signal.NONE, f"trend {trend}, waiting for " + " + ".join(missing), fast_now, slow_now, 0, close_now
        )
