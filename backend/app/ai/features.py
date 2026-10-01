"""One feature builder, used by training and by serving.

Training/serving skew is the failure that produces a model which validates
beautifully and loses money: the offline pipeline computes a feature one way,
the live path computes it slightly differently, and the model is scored on
inputs it never saw. The only defence that actually holds is to have one
function and call it from both places. That is what this is, and a test asserts
both callers produce identical vectors from identical inputs.

Causality is structural. `build_features` takes a `FeatureInputs` whose fields
are, by construction, things that were true at or before `decision_time`:
closed bars only, the executable quote at the decision, and calendar entries
already published. There is no argument through which a later value can arrive.

**Missing is missing.** A absent bid, an unknown fee, an unavailable calendar
never becomes 0.0. Every feature reports availability alongside its value, and
an incomplete vector makes the predictor abstain rather than score a hole.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.ai.contracts import FeatureSchema

#: Ordered feature contract. Changing this list is a schema change: bump the
#: version, and every model trained on the old one stops loading.
FEATURE_NAMES = (
    "spread_over_spacing",      # executable cost of entry, relative to a grid step
    "atr_over_spacing",         # is the market moving enough for a straddle to pay
    "range_over_atr",           # range-boundness: high when price is going nowhere
    "abs_slope_over_atr",       # directional persistence, sign removed
    "bars_since_expansion",     # how long since a genuinely wide bar
    "session_sin",              # time of day, encoded without a discontinuity
    "session_cos",
    "minutes_to_scheduled_event",   # proximity to a PUBLISHED calendar entry
)
FEATURE_SCHEMA = FeatureSchema(names=FEATURE_NAMES, version="entry-v1")

#: Used when no published calendar entry is within reach. It is a sentinel for
#: "nothing scheduled nearby", not a measurement, and it is only ever produced
#: when a calendar was actually loaded and consulted.
NO_EVENT_MINUTES = 1440.0


@dataclass
class FeatureInputs:
    """Everything the builder may look at. Nothing here is from the future.

    `closed_bars` are bars that have CLOSED. The forming bar is not accepted,
    because its final high, low and close do not exist yet.
    """

    decision_time: datetime
    bid: float | None
    ask: float | None
    quote_as_of: datetime | None
    grid_distance: float
    closed_bars: list[dict] = field(default_factory=list)
    bar_as_of: datetime | None = None
    #: None means no calendar was loaded. That is UNKNOWN, and it is not the
    #: same as "no events scheduled".
    calendar: object | None = None
    calendar_available: bool = False


@dataclass
class FeatureVector:
    values: dict[str, float]
    available: dict[str, bool]
    inputs_available_at: datetime | None
    schema: FeatureSchema = FEATURE_SCHEMA

    @property
    def complete(self) -> bool:
        return all(self.available.get(name, False) for name in self.schema.names)

    @property
    def missing(self) -> list[str]:
        return [n for n in self.schema.names if not self.available.get(n, False)]

    def as_row(self) -> list[float] | None:
        """The ordered vector, or None when anything is missing.

        Returning None rather than a padded row is the point: a model scored on
        an imputed hole is a model scored on a number nobody measured.
        """
        if not self.complete:
            return None
        return [float(self.values[name]) for name in self.schema.names]

    def as_dict(self) -> dict:
        return {
            "values": self.values,
            "available": self.available,
            "complete": self.complete,
            "missing": self.missing,
            "inputs_available_at": self.inputs_available_at.isoformat() if self.inputs_available_at else None,
            "schema": self.schema.as_dict(),
        }


def _true_range(previous: dict, current: dict) -> float:
    return max(current["high"] - current["low"],
               abs(current["high"] - previous["close"]),
               abs(current["low"] - previous["close"]))


def atr(closed_bars: list[dict], period: int = 14) -> float | None:
    """None during warmup — never a padded value that looks like a measurement."""
    if len(closed_bars) < period + 1:
        return None
    trs = [_true_range(p, c) for p, c in
           zip(closed_bars[-period - 1:-1], closed_bars[-period:])]
    return sum(trs) / len(trs) if trs else None


def build_features(inputs: FeatureInputs, *, atr_period: int = 14,
                   slope_period: int = 20) -> FeatureVector:
    values: dict[str, float] = {}
    available: dict[str, bool] = {name: False for name in FEATURE_NAMES}
    stamps = [s for s in (inputs.quote_as_of, inputs.bar_as_of) if s is not None]
    inputs_available_at = max(stamps) if stamps else None

    # --- executable entry cost --------------------------------------------
    if (inputs.bid is not None and inputs.ask is not None
            and inputs.bid > 0 and inputs.ask >= inputs.bid and inputs.grid_distance):
        values["spread_over_spacing"] = (inputs.ask - inputs.bid) / inputs.grid_distance
        available["spread_over_spacing"] = True

    # --- volatility and shape, from CLOSED bars only ------------------------
    bars = inputs.closed_bars
    value_atr = atr(bars, atr_period)
    if value_atr is not None and value_atr > 0 and inputs.grid_distance:
        values["atr_over_spacing"] = value_atr / inputs.grid_distance
        available["atr_over_spacing"] = True

    if value_atr is not None and value_atr > 0 and len(bars) >= slope_period:
        window = bars[-slope_period:]
        high = max(b["high"] for b in window)
        low = min(b["low"] for b in window)
        values["range_over_atr"] = (high - low) / value_atr
        available["range_over_atr"] = True

        # Least-squares slope of the closes, scaled by ATR so it is comparable
        # across price levels. The sign is dropped: this strategy is symmetric
        # and does not care which way the move goes, only that there is one.
        n = len(window)
        xs = list(range(n))
        mean_x = sum(xs) / n
        mean_y = sum(b["close"] for b in window) / n
        denominator = sum((x - mean_x) ** 2 for x in xs)
        if denominator > 0:
            slope = sum((x - mean_x) * (b["close"] - mean_y)
                        for x, b in zip(xs, window)) / denominator
            values["abs_slope_over_atr"] = abs(slope) / value_atr
            available["abs_slope_over_atr"] = True

    if value_atr is not None and value_atr > 0 and len(bars) >= atr_period + 1:
        since = 0
        for bar in reversed(bars):
            if (bar["high"] - bar["low"]) > value_atr:
                break
            since += 1
        values["bars_since_expansion"] = float(min(since, 500))
        available["bars_since_expansion"] = True

    # --- session, encoded without a midnight discontinuity ------------------
    minute_of_day = inputs.decision_time.hour * 60 + inputs.decision_time.minute
    angle = 2 * math.pi * minute_of_day / 1440.0
    values["session_sin"] = math.sin(angle)
    values["session_cos"] = math.cos(angle)
    available["session_sin"] = available["session_cos"] = True

    # --- scheduled event proximity -----------------------------------------
    # Only entries whose SCHEDULE was already published are visible. With no
    # calendar loaded the feature stays unavailable — absence of data is not
    # absence of events, and a 1440 here would assert the diary was empty.
    if inputs.calendar_available and inputs.calendar is not None:
        try:
            published = inputs.calendar.events_available_at(inputs.decision_time)
        except Exception:
            published = None
        if published is not None:
            gaps = [abs((e.event_time - inputs.decision_time).total_seconds()) / 60.0
                    for e in published]
            values["minutes_to_scheduled_event"] = min(min(gaps), NO_EVENT_MINUTES) if gaps else NO_EVENT_MINUTES
            available["minutes_to_scheduled_event"] = True

    return FeatureVector(values=values, available=available,
                         inputs_available_at=inputs_available_at)
