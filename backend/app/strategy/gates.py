"""The Phase C candidate entry gates.

Each one carries a falsifiable hypothesis in its docstring. A gate that cannot
be shown to be wrong is not a hypothesis, it is a decoration.

Every gate is causal by construction: it takes an explicit `as_of` timestamp
and may only read values the context says were available at or before it. The
replay and the runtime call the same objects, so a gate cannot behave one way
in evaluation and another way live.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.strategy.admission import Gate


class ExecutionQualityGate(Gate):
    """Candidate 1.

    Hypothesis: a meaningful share of this strategy's losses are explained by
    starting baskets under expensive or unreliable entry conditions — a wide
    spread, or a quote too old to act on.

    Falsified if: filtering those conditions out does not reduce net loss per
    basket relative to the baseline over the same period.

    Why the spread matters here specifically. The strategy pays the spread on
    every one of up to 20 fills, and its basket target is a fixed cash amount.
    At 0.01 lots and a $10 target, a 24-point spread costs about $6.00 across a
    full grid — 60% of the target — before price has moved at all. The
    threshold is therefore expressed against the target and the spacing rather
    than as a bare point count, so it stays meaningful if either changes.
    """

    name = "execution_quality"
    data_required = True

    def __init__(self, max_spread_fraction_of_spacing: float = 1.0,
                 max_quote_age_ms: float = 2000.0):
        # Default: the spread may not exceed one grid step. At 0.30 spacing and
        # 0.01 price points that is 30 points — already wide for gold. This is a
        # starting value to be fitted on development data, not a claim.
        self.max_spread_fraction_of_spacing = max_spread_fraction_of_spacing
        self.max_quote_age_ms = max_quote_age_ms

    def evaluate(self, ctx: dict):
        as_of = ctx.get("quote_as_of")
        bid, ask = ctx.get("bid"), ctx.get("ask")
        spacing = ctx.get("grid_distance")
        age_ms = ctx.get("quote_age_ms")

        if bid is None or ask is None:
            return self.unknown("no valid bid/ask available", as_of)
        if not (bid > 0 and ask > 0 and ask >= bid):
            return self.unknown(f"bid/ask not usable (bid={bid}, ask={ask})", as_of)
        if not spacing:
            return self.unknown("grid spacing unknown", as_of)
        if age_ms is None:
            return self.unknown("quote age unknown", as_of)
        if age_ms > self.max_quote_age_ms:
            return self.block(
                f"quote is {age_ms:.0f} ms old (limit {self.max_quote_age_ms:.0f} ms)", as_of,
                quote_age_ms=age_ms,
            )

        spread = ask - bid
        limit = spacing * self.max_spread_fraction_of_spacing
        if spread > limit:
            return self.block(
                f"spread {spread:.3f} exceeds {self.max_spread_fraction_of_spacing:.2f}x "
                f"the {spacing:.2f} grid step",
                as_of, spread=spread, limit=limit,
            )
        return self.allow(f"spread {spread:.3f} within {limit:.3f}", as_of, spread=spread)


class RegimeGate(Gate):
    """Candidate 2.

    Hypothesis: this strategy is a symmetric BREAKOUT STRADDLE, not a mean
    reversion grid. Its buy stops sit above the reference and its sell stops
    below, so both sides fill on movement *away* from where the basket started.
    It reaches its target on a sustained one-directional move of roughly 2.9
    price units, and it freezes at about -$37.80 when price oscillates enough to
    fill both sides and returns. Therefore an expanding, directional regime
    should help it and a tight range should hurt it — which is the opposite of
    the usual claim that grids need ranging markets.

    Falsified if: admitting only in the "expanding" regime does not improve net
    result per basket, or if the regime feature has no relationship to whether
    a basket reaches its target or freezes.

    Causality. Every feature comes from CLOSED candles only. The current bar's
    final high, low and close do not exist yet while that bar is forming, so
    using them would be reading the answer. `ctx["closed_bars"]` is the closed
    history and `ctx["bar_as_of"]` is the close time of the most recent one.
    """

    name = "regime"
    data_required = True

    def __init__(self, atr_period: int = 14, min_atr_multiple_of_spacing: float = 2.0):
        # The basket needs ~2.9 price units of one-way movement to reach a $10
        # target at 0.30 spacing. Requiring recent ATR to be at least this
        # multiple of the spacing is the minimal expression of "the market is
        # moving enough for this structure to pay". The multiple is a parameter
        # to be fitted on development data, not an asserted edge.
        self.atr_period = atr_period
        self.min_atr_multiple_of_spacing = min_atr_multiple_of_spacing

    @staticmethod
    def atr(closed_bars, period: int) -> float | None:
        """Average true range over CLOSED bars. None when there are too few.

        Warmup is handled by returning None rather than by padding: a padded
        value early in the series is a number with no information in it.
        """
        if not closed_bars or len(closed_bars) < period + 1:
            return None
        trs = []
        for prev, cur in zip(closed_bars[-period - 1:-1], closed_bars[-period:]):
            high, low, prev_close = cur["high"], cur["low"], prev["close"]
            trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        return sum(trs) / len(trs) if trs else None

    def evaluate(self, ctx: dict):
        as_of = ctx.get("bar_as_of")
        bars = ctx.get("closed_bars")
        spacing = ctx.get("grid_distance")
        if not bars:
            return self.unknown("no closed bars available", as_of)
        if not spacing:
            return self.unknown("grid spacing unknown", as_of)

        value = self.atr(bars, self.atr_period)
        if value is None:
            return self.unknown(
                f"ATR({self.atr_period}) needs {self.atr_period + 1} closed bars, "
                f"have {len(bars)} — warmup incomplete", as_of,
            )
        required = spacing * self.min_atr_multiple_of_spacing
        if value < required:
            return self.block(
                f"ATR({self.atr_period}) {value:.3f} is below {self.min_atr_multiple_of_spacing:.1f}x "
                f"the {spacing:.2f} spacing ({required:.3f}): too quiet for a breakout straddle, "
                "and a tight range is what freezes this basket",
                as_of, atr=value, required=required,
            )
        return self.allow(f"ATR({self.atr_period}) {value:.3f} at or above {required:.3f}",
                          as_of, atr=value, required=required)


class EventBlackoutGate(Gate):
    """Candidate 3.

    Hypothesis: starting a basket immediately around a scheduled high-impact
    release is worse than average, because the spread widens and price gaps
    through levels rather than trading through them.

    Falsified if: excluding those windows does not reduce loss per basket or
    drawdown over the same period.

    What this is NOT. A calendar says a number is due at a time. It says
    nothing about which way gold will go, and nothing here predicts direction.
    Only *scheduled* timing is used, and only timing that was knowable in
    advance: each event carries `available_from`, the moment the schedule entry
    itself was published. An event whose schedule was not yet available at the
    decision time is invisible to the gate, because using it would be reading
    tomorrow's calendar today.

    Missing coverage is UNKNOWN, never "no events". `data_required` is True, so
    a profile using this gate stops admitting while its calendar is absent
    rather than trading as though the diary were empty.
    """

    name = "event_blackout"
    data_required = True

    def __init__(self, before: timedelta = timedelta(minutes=30),
                 after: timedelta = timedelta(minutes=30),
                 impacts: tuple[str, ...] = ("high",)):
        self.before = before
        self.after = after
        self.impacts = tuple(i.lower() for i in impacts)

    def evaluate(self, ctx: dict):
        now = ctx.get("now")
        calendar = ctx.get("calendar")
        if now is None:
            return self.unknown("no decision time supplied")
        if calendar is None:
            return self.unknown(
                "no economic calendar loaded — absence of data is not absence of events", now,
            )
        covered_until = getattr(calendar, "covered_until", None)
        if covered_until is not None and now > covered_until:
            return self.unknown(
                f"calendar coverage ends {covered_until.isoformat()}, which is before "
                f"{now.isoformat()}", now,
            )

        for event in calendar.events_available_at(now):
            if event.impact.lower() not in self.impacts:
                continue
            start = event.event_time - self.before
            end = event.event_time + self.after
            if start <= now <= end:
                return self.block(
                    f"{event.title} ({event.impact}) at {event.event_time.isoformat()} — "
                    f"inside the {self.before} / {self.after} blackout",
                    now, event=event.title, event_time=event.event_time.isoformat(),
                )
        return self.allow("no scheduled high-impact event inside the blackout window", now)
