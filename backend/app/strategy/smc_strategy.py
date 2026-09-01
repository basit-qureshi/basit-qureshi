"""Smart Money Concepts strategy: M15 structure -> M1 shift -> M5 entry.

The chain of conditions, in the order the strategy checks them:

1. M15: find the most recent Break of Structure (BOS) and take its direction
   as the trend.
2. M15: locate the order block that produced that BOS - the last opposite
   candle before the impulse - and require a Fair Value Gap inside the impulse
   that followed it. No FVG, no order block.
3. M15: the order block must sit on the right side of the dealing range -
   discount for a bullish setup, premium for a bearish one.
4. Wait for price to trade back into that M15 order block.
5. M1: once price is in the zone, require a Market Structure Shift in the
   trade's direction. This is the trigger that says the pullback is finished.
6. M5: mark the extreme candle of the leg the MSS started from - the lowest
   candle for a long, the highest for a short. That candle is the entry zone.
7. Entry at the near edge of that M5 zone, stop beyond its far edge, target at
   the structural extreme the BOS ran to.

Two failure modes reported from trading this by hand shaped the design:

- Price often reached the target without ever tapping the M5 zone, so the
  trade was missed. The setup therefore carries a fallback trigger: if price
  runs far enough in the trade's direction without ever coming back for the
  entry, the engine takes it at market instead, provided the reward:risk that
  remains is still worth taking.
- Stops placed exactly at the order block's edge were being taken out before
  the move. On gold the spread alone is 24-30 points, so the stop is pushed
  beyond the zone by the spread plus a buffer rather than sitting on it.
"""

from dataclasses import dataclass

import pandas as pd

from app.strategy.ema_rsi_strategy import Signal


@dataclass
class SmcSetup:
    """A fully-specified trade plan, or an explanation of what is still missing."""

    signal: Signal
    reason: str
    stage: str = "scanning"
    entry: float = 0.0  # price to wait for (near edge of the M5 order block)
    sl: float = 0.0
    tp: float = 0.0
    fallback_price: float = 0.0  # enter at market if price gets here without tapping
    rr: float = 0.0
    zone_low: float = 0.0  # M5 entry zone, used to invalidate the setup
    zone_high: float = 0.0


def _swing_high_indices(df: pd.DataFrame, n: int) -> list[int]:
    """Positional indices of pivot highs: a high with `n` lower highs each side."""
    highs = df["high"].to_numpy()
    out = []
    for i in range(n, len(highs) - n):
        window = highs[i - n : i + n + 1]
        if highs[i] == window.max() and highs[i] > highs[i - 1] and highs[i] >= highs[i + 1]:
            out.append(i)
    return out


def _swing_low_indices(df: pd.DataFrame, n: int) -> list[int]:
    lows = df["low"].to_numpy()
    out = []
    for i in range(n, len(lows) - n):
        window = lows[i - n : i + n + 1]
        if lows[i] == window.min() and lows[i] < lows[i - 1] and lows[i] <= lows[i + 1]:
            out.append(i)
    return out


def _find_bos(df: pd.DataFrame, n: int) -> tuple[int, str, float, int] | None:
    """The most recent Break of Structure.

    Returns (break_bar, direction, broken_level, broken_swing_bar). A swing can
    only be counted as broken by a candle that closes past it after the swing
    itself is confirmed, which takes `n` bars.
    """
    close = df["close"].to_numpy()
    best: tuple[int, str, float, int] | None = None

    for i in _swing_high_indices(df, n):
        level = float(df["high"].iloc[i])
        for j in range(i + n + 1, len(df)):
            if close[j] > level:
                if best is None or j > best[0]:
                    best = (j, "bullish", level, i)
                break

    for i in _swing_low_indices(df, n):
        level = float(df["low"].iloc[i])
        for j in range(i + n + 1, len(df)):
            if close[j] < level:
                if best is None or j > best[0]:
                    best = (j, "bearish", level, i)
                break

    return best


def _find_order_block(
    df: pd.DataFrame, break_bar: int, direction: str, lookback: int, fvg_window: int
) -> tuple[int, float, float] | None:
    """The order block behind a BOS: the last opposite-colour candle before the
    impulse, and only if that impulse left a Fair Value Gap.

    Returns (bar_index, zone_low, zone_high).
    """
    open_ = df["open"].to_numpy()
    close = df["close"].to_numpy()
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    start = max(0, break_bar - lookback)

    for k in range(break_bar, start, -1):
        is_candidate = close[k] < open_[k] if direction == "bullish" else close[k] > open_[k]
        if not is_candidate:
            continue
        # The gap must belong to the impulse that left this candle behind, so
        # only look a few candles forward from it.
        for m in range(k + 1, min(k + 1 + fvg_window, len(df) - 1)):
            if direction == "bullish" and low[m + 1] > high[m - 1]:
                return k, float(low[k]), float(high[k])
            if direction == "bearish" and high[m + 1] < low[m - 1]:
                return k, float(high[k]), float(low[k])
    return None


def _find_mss(df: pd.DataFrame, direction: str, n: int, max_age: int) -> int | None:
    """Bar index of a recent Market Structure Shift in `direction`, or None.

    For a long that means a close above the last pivot high, which says the
    pullback that brought price into the zone has stopped making lower highs.
    """
    close = df["close"].to_numpy()
    pivots = _swing_high_indices(df, n) if direction == "bullish" else _swing_low_indices(df, n)
    newest: int | None = None
    for i in pivots:
        level = float(df["high"].iloc[i] if direction == "bullish" else df["low"].iloc[i])
        for j in range(i + n + 1, len(df)):
            broke = close[j] > level if direction == "bullish" else close[j] < level
            if broke:
                if newest is None or j > newest:
                    newest = j
                break
    if newest is None or (len(df) - 1 - newest) > max_age:
        return None
    return newest


def _entry_zone(df: pd.DataFrame, direction: str, lookback: int) -> tuple[float, float] | None:
    """The M5 entry zone: the extreme candle of the leg the shift came from -
    lowest candle for a long, highest for a short. Returns (zone_low, zone_high)."""
    if len(df) < 2:
        return None
    recent = df.iloc[-lookback:] if lookback and len(df) > lookback else df
    bar = recent.loc[recent["low"].idxmin()] if direction == "bullish" else recent.loc[recent["high"].idxmax()]
    return float(bar["low"]), float(bar["high"])


class SmcStrategy:
    """See the module docstring for the full sequence."""

    # The engine reads these to know which candle sets to fetch and that this
    # strategy prices its own stop and target instead of using fixed pips.
    is_structural = True
    required_timeframes = ("M15", "M5", "M1")

    SENSITIVITY_PRESETS = {
        # swing_n: how many bars either side define a pivot. Smaller sees more
        # structure (more setups, more noise); larger only reacts to obvious swings.
        "aggressive": dict(swing_n=1, mss_swing_n=1, min_rr=1.5, zone_tolerance_points=40),
        "balanced": dict(swing_n=2, mss_swing_n=2, min_rr=2.0, zone_tolerance_points=20),
        "conservative": dict(swing_n=3, mss_swing_n=2, min_rr=2.5, zone_tolerance_points=10),
    }

    @classmethod
    def from_sensitivity(cls, sensitivity: str, **overrides) -> "SmcStrategy":
        preset = cls.SENSITIVITY_PRESETS.get(sensitivity, cls.SENSITIVITY_PRESETS["balanced"])
        return cls(**{**preset, **overrides})

    def __init__(
        self,
        swing_n: int = 2,
        mss_swing_n: int = 2,
        min_rr: float = 2.0,
        zone_tolerance_points: float = 20,
        sl_buffer_points: float = 20,
        fallback_points: float = 150,
        fallback_min_rr: float = 1.0,
        ob_lookback: int = 30,
        fvg_window: int = 3,
        mss_max_age: int = 15,
        m5_zone_lookback: int = 12,
    ):
        self.swing_n = swing_n
        self.mss_swing_n = mss_swing_n
        self.min_rr = min_rr
        self.zone_tolerance_points = zone_tolerance_points
        self.sl_buffer_points = sl_buffer_points
        self.fallback_points = fallback_points
        self.fallback_min_rr = fallback_min_rr
        self.ob_lookback = ob_lookback
        self.fvg_window = fvg_window
        self.mss_max_age = mss_max_age
        self.m5_zone_lookback = m5_zone_lookback
        # Kept so the chart overlay and backtest sizing keep working unchanged.
        self.ema_fast_period = 9
        self.ema_slow_period = 21
        self.rsi_period = 14

    def _dealing_range(self, m15: pd.DataFrame, break_bar: int, direction: str) -> tuple[float, float]:
        """The leg the BOS belongs to: its origin swing, and the extreme reached
        after the break. Falls back to the lookback window when no swing formed
        before the break — early in the series there may not be one yet."""
        fallback_start = max(0, break_bar - self.ob_lookback)
        if direction == "bullish":
            origins = [i for i in _swing_low_indices(m15, self.swing_n) if i < break_bar]
            low = float(m15["low"].iloc[origins[-1]]) if origins else float(
                m15["low"].iloc[fallback_start : break_bar + 1].min()
            )
            return low, float(m15["high"].iloc[break_bar:].max())
        origins = [i for i in _swing_high_indices(m15, self.swing_n) if i < break_bar]
        high = float(m15["high"].iloc[origins[-1]]) if origins else float(
            m15["high"].iloc[fallback_start : break_bar + 1].max()
        )
        return float(m15["low"].iloc[break_bar:].min()), high

    def generate_setup(
        self,
        m15: pd.DataFrame,
        m5: pd.DataFrame,
        m1: pd.DataFrame,
        price: float,
        pip_size: float,
        spread: float = 0.0,
    ) -> SmcSetup:
        if len(m15) < 30 or len(m5) < 20 or len(m1) < 20:
            return SmcSetup(Signal.NONE, "not enough candles yet", stage="warming up")

        bos = _find_bos(m15, self.swing_n)
        if bos is None:
            return SmcSetup(Signal.NONE, "no M15 break of structure found", stage="no BOS")
        break_bar, direction, broken_level, _ = bos

        ob = _find_order_block(m15, break_bar, direction, self.ob_lookback, self.fvg_window)
        if ob is None:
            return SmcSetup(
                Signal.NONE,
                f"M15 {direction} BOS found, but no order block with an FVG behind it",
                stage="no OB+FVG",
            )
        _, ob_low, ob_high = ob

        # Premium/discount is measured across the leg that produced the BOS:
        # from the swing the move started at, to the extreme it reached after
        # breaking structure. Measuring from the lowest low in an arbitrary
        # lookback instead drags the midpoint far away from the actual leg and
        # makes order blocks in a perfectly good discount read as premium.
        range_low, range_high = self._dealing_range(m15, break_bar, direction)
        equilibrium = (range_low + range_high) / 2
        ob_mid = (ob_low + ob_high) / 2
        in_right_half = ob_mid <= equilibrium if direction == "bullish" else ob_mid >= equilibrium
        if not in_right_half:
            side = "discount" if direction == "bullish" else "premium"
            return SmcSetup(
                Signal.NONE, f"M15 order block is not in {side} — skipping", stage="OB on wrong side"
            )

        tolerance = self.zone_tolerance_points * pip_size
        in_zone = (ob_low - tolerance) <= price <= (ob_high + tolerance)
        if not in_zone:
            return SmcSetup(
                Signal.NONE,
                f"waiting for price to reach the M15 {direction} order block "
                f"({ob_low:.2f}–{ob_high:.2f}), now {price:.2f}",
                stage="waiting for OB",
            )

        mss_bar = _find_mss(m1, direction, self.mss_swing_n, self.mss_max_age)
        if mss_bar is None:
            return SmcSetup(
                Signal.NONE,
                "price is in the M15 order block, waiting for an M1 market structure shift",
                stage="waiting for MSS",
            )
        mss_price = float(m1["close"].iloc[mss_bar])

        zone = _entry_zone(m5, direction, self.m5_zone_lookback)
        if zone is None:
            return SmcSetup(Signal.NONE, "could not mark an M5 entry candle", stage="no M5 zone")
        zone_low, zone_high = zone

        # The stop clears the zone by the spread plus a buffer. Sitting exactly
        # on the zone's edge is what was getting picked off before the move.
        buffer = spread + self.sl_buffer_points * pip_size
        if direction == "bullish":
            entry = zone_high  # near edge: price taps here first on the way down
            sl = zone_low - buffer
            tp = float(m15["high"].iloc[break_bar:].max())
            fallback_price = mss_price + self.fallback_points * pip_size
            signal = Signal.BUY
        else:
            entry = zone_low
            sl = zone_high + buffer
            tp = float(m15["low"].iloc[break_bar:].min())
            fallback_price = mss_price - self.fallback_points * pip_size
            signal = Signal.SELL

        risk = abs(entry - sl)
        reward = abs(tp - entry)
        if risk <= 0:
            return SmcSetup(Signal.NONE, "M5 zone has no height — cannot place a stop", stage="bad zone")
        rr = reward / risk
        target_beyond_entry = tp > entry if direction == "bullish" else tp < entry
        if not target_beyond_entry:
            return SmcSetup(
                Signal.NONE,
                f"structural target {tp:.2f} is already behind the entry {entry:.2f}",
                stage="target passed",
            )
        if rr < self.min_rr:
            return SmcSetup(
                Signal.NONE,
                f"setup found but reward:risk is only 1:{rr:.1f} (need 1:{self.min_rr:.1f})",
                stage="RR too low",
            )

        return SmcSetup(
            signal=signal,
            reason=(
                f"M15 {direction} BOS over {broken_level:.2f}, OB+FVG tapped, M1 MSS confirmed — "
                f"entry {entry:.2f}, SL {sl:.2f}, TP {tp:.2f} (1:{rr:.1f})"
            ),
            stage="setup ready",
            entry=entry,
            sl=sl,
            tp=tp,
            fallback_price=fallback_price,
            rr=rr,
            zone_low=zone_low,
            zone_high=zone_high,
        )
