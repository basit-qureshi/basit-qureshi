import pandas as pd

from app.strategy.ema_rsi_strategy import Signal, StrategyResult

_TIMEFRAME_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


class RetestRejectionStrategy:
    """Multi-timeframe scalping: breakout on M5, retest + rejection on M1.

    The sequence it waits for, in order:

    1. On the higher timeframe (M5 by default), price breaks out of its recent
       range — a candle closes above the range high, or below the range low.
       That broken level becomes the level to watch.
    2. On the entry timeframe (M1), price pulls back to that level.
    3. The pullback gets rejected: the candle wicks into the level but closes
       back on the breakout side, leaving a rejection wick. That rejection is
       the entry.

    Entering on the rejection rather than the breakout itself means the stop
    sits just past a level the market has already defended once, instead of
    chasing the initial spike.

    Only closed candles are evaluated — the still-forming candle is ignored,
    since a "rejection" that hasn't closed yet can still turn into a break.
    """

    higher_timeframe = "M5"

    def __init__(
        self,
        range_lookback: int = 12,
        breakout_window: int = 6,
        wick_ratio: float = 0.3,
        volume_factor: float = 1.0,
        tolerance_factor: float = 0.6,
    ):
        self.range_lookback = range_lookback
        self.breakout_window = breakout_window
        self.wick_ratio = wick_ratio
        self.volume_factor = volume_factor
        self.tolerance_factor = tolerance_factor
        # Kept so the /api/candles chart overlay and settings stay meaningful
        # even though this strategy doesn't trade EMA crosses.
        self.ema_fast_period = 5
        self.ema_slow_period = 13
        self.rsi_period = 14

    def _resample(self, candles: pd.DataFrame, minutes: int) -> pd.DataFrame:
        """Backtests feed a single timeframe, so build the higher one from it."""
        if not isinstance(candles.index, pd.DatetimeIndex):
            return candles
        agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        return candles.resample(f"{minutes}min").agg(agg).dropna()

    def _find_breakout(self, higher: pd.DataFrame) -> tuple[str, float] | None:
        """Returns ("up"|"down", broken level) for the most recent breakout, if any."""
        needed = self.range_lookback + self.breakout_window
        if len(higher) < needed:
            return None

        range_candles = higher.iloc[-needed:-self.breakout_window]
        resistance = float(range_candles["high"].max())
        support = float(range_candles["low"].min())
        recent = higher.iloc[-self.breakout_window :]

        up_idx = down_idx = None
        for i in range(len(recent)):
            close = float(recent["close"].iloc[i])
            if close > resistance:
                up_idx = i
            if close < support:
                down_idx = i

        if up_idx is None and down_idx is None:
            return None
        if down_idx is None or (up_idx is not None and up_idx >= down_idx):
            return "up", resistance
        return "down", support

    def generate_signal(self, candles: pd.DataFrame, higher_candles: pd.DataFrame | None = None) -> StrategyResult:
        close_series = candles["close"]
        if len(candles) < 25:
            return StrategyResult(Signal.NONE, "not enough candles", 0, 0, 0, float(close_series.iloc[-1]))

        if higher_candles is None or higher_candles.empty:
            higher_candles = self._resample(candles, _TIMEFRAME_MINUTES.get(self.higher_timeframe, 5))

        # Evaluate the last CLOSED candle; the final row is still forming.
        entry_candle = candles.iloc[-2]
        close_now = float(entry_candle["close"])
        high = float(entry_candle["high"])
        low = float(entry_candle["low"])
        open_ = float(entry_candle["open"])
        candle_range = high - low

        breakout = self._find_breakout(higher_candles)
        if breakout is None:
            return StrategyResult(Signal.NONE, f"no {self.higher_timeframe} breakout yet", 0, 0, 0, close_now)

        direction, level = breakout

        avg_range = float((candles["high"] - candles["low"]).iloc[-21:-1].mean())
        tolerance = avg_range * self.tolerance_factor

        avg_volume = float(candles["volume"].iloc[-21:-1].mean())
        volume_ok = avg_volume <= 0 or float(entry_candle["volume"]) >= avg_volume * self.volume_factor

        if candle_range <= 0:
            return StrategyResult(Signal.NONE, "flat candle", 0, 0, 0, close_now)

        if direction == "up":
            retested = low <= level + tolerance
            closed_above = close_now > level
            bullish = close_now > open_
            lower_wick = min(open_, close_now) - low
            rejected = lower_wick >= candle_range * self.wick_ratio
            if retested and closed_above and bullish and rejected and volume_ok:
                return StrategyResult(
                    Signal.BUY,
                    f"{self.higher_timeframe} breakout up @ {level:.2f}, M1 retest rejected",
                    0,
                    0,
                    0,
                    close_now,
                )
            reason = self._pending_reason(retested, closed_above and bullish, rejected, volume_ok, level, "support")
        else:
            retested = high >= level - tolerance
            closed_below = close_now < level
            bearish = close_now < open_
            upper_wick = high - max(open_, close_now)
            rejected = upper_wick >= candle_range * self.wick_ratio
            if retested and closed_below and bearish and rejected and volume_ok:
                return StrategyResult(
                    Signal.SELL,
                    f"{self.higher_timeframe} breakdown @ {level:.2f}, M1 retest rejected",
                    0,
                    0,
                    0,
                    close_now,
                )
            reason = self._pending_reason(retested, closed_below and bearish, rejected, volume_ok, level, "resistance")

        return StrategyResult(Signal.NONE, reason, 0, 0, 0, close_now)

    def _pending_reason(
        self, retested: bool, closed_right_side: bool, rejected: bool, volume_ok: bool, level: float, role: str
    ) -> str:
        if not retested:
            return f"breakout held, waiting for retest of {level:.2f} ({role})"
        missing = []
        if not closed_right_side:
            missing.append("close back through level")
        if not rejected:
            missing.append("rejection wick")
        if not volume_ok:
            missing.append("volume")
        return f"retesting {level:.2f}, waiting for " + " + ".join(missing)
