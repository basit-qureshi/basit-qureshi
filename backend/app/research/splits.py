"""Chronological splitting, on time — not on row count.

Two failure modes this exists to prevent.

**Equal rows are not equal time.** Ticks are irregular: a thousand rows during
a release is seconds, a thousand rows overnight is hours. Splitting by row
index produces windows of wildly different duration and lets a "20% test set"
be forty minutes of one busy afternoon. Every boundary here is a timestamp.

**Overlapping outcomes leak across boundaries.** A basket opened before a
boundary and closed after it spans both sides. Its outcome is partly determined
by data in the other window, so counting it in either one leaks. Baskets that
straddle a boundary are PURGED, and an additional embargo is applied after the
boundary so a basket opened moments before it cannot influence the next window
through sheer proximity.

scikit-learn's TimeSeriesSplit shows the chronological shape and a gap, but it
indexes rows and knows nothing about trade durations, so it solves neither
problem by itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Window:
    name: str
    start: datetime
    end: datetime          # exclusive

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "start_utc": self.start.isoformat(),
            "end_utc": self.end.isoformat(),
            "hours": round(self.duration.total_seconds() / 3600, 2),
        }


@dataclass(frozen=True)
class SplitPlan:
    development: Window
    validation: tuple[Window, ...]
    final_evaluation: Window
    embargo: timedelta

    def as_dict(self) -> dict:
        return {
            "development": self.development.as_dict(),
            "validation": [w.as_dict() for w in self.validation],
            "final_evaluation": self.final_evaluation.as_dict(),
            "embargo_minutes": round(self.embargo.total_seconds() / 60, 1),
        }


def chronological_split(start: datetime, end: datetime, *,
                        development_fraction: float = 0.5,
                        validation_folds: int = 3,
                        embargo: timedelta = timedelta(hours=6)) -> SplitPlan:
    """Development, rolling validation folds, then one final untouched window.

    The final window is last in time and is never used for fitting anything.
    Thresholds and feature transforms are fitted on `development` only.
    """
    if end <= start:
        raise ValueError("end must be after start")
    if not 0 < development_fraction < 1:
        raise ValueError("development_fraction must be between 0 and 1")
    if validation_folds < 1:
        raise ValueError("at least one validation fold is required")

    total = end - start
    dev_end = start + total * development_fraction
    development = Window("development", start, dev_end)

    # The remainder splits into validation folds plus one final window.
    remaining = end - dev_end
    piece = remaining / (validation_folds + 1)
    folds = []
    cursor = dev_end
    for i in range(validation_folds):
        folds.append(Window(f"validation-{i + 1}", cursor, cursor + piece))
        cursor = cursor + piece
    final = Window("final_evaluation", cursor, end)
    return SplitPlan(development, tuple(folds), final, embargo)


@dataclass(frozen=True)
class BasketOutcome:
    """One basket, with the span its result actually depends on."""

    basket_id: str
    opened_at: datetime
    closed_at: datetime | None
    net_result: float | None
    #: True when the basket was still open when the data ran out. Dropping
    #: these would quietly remove exactly the baskets that never recovered.
    still_open: bool = False


def assign_to_window(outcome: BasketOutcome, window: Window,
                     embargo: timedelta) -> tuple[bool, str]:
    """Whether a basket may be counted in `window`, and why not if not.

    A basket counts only when its ENTIRE span — open to close — sits inside the
    window, and only when it opened at least `embargo` before the window's end.
    """
    if not window.contains(outcome.opened_at):
        return False, "opened outside the window"
    if outcome.closed_at is None or outcome.still_open:
        return False, "never closed within the data — reported as remaining exposure, not dropped"
    if outcome.closed_at >= window.end:
        return False, "straddles the window boundary — purged to avoid leakage"
    if outcome.opened_at > window.end - embargo:
        return False, f"opened inside the {embargo} embargo before the boundary"
    return True, "inside the window"


def partition(outcomes, plan: SplitPlan) -> dict:
    """Assigns every basket to a window, and records every exclusion.

    Exclusions are returned rather than discarded: a silent drop is how a
    losing basket disappears from a result.
    """
    windows = [plan.development, *plan.validation, plan.final_evaluation]
    assigned = {w.name: [] for w in windows}
    excluded: list[dict] = []
    for outcome in outcomes:
        placed = False
        for window in windows:
            ok, why = assign_to_window(outcome, window, plan.embargo)
            if ok:
                assigned[window.name].append(outcome)
                placed = True
                break
        if not placed:
            excluded.append({
                "basket_id": outcome.basket_id,
                "opened_at": outcome.opened_at.isoformat(),
                "closed_at": outcome.closed_at.isoformat() if outcome.closed_at else None,
                "reason": _first_reason(outcome, windows, plan.embargo),
            })
    return {"assigned": assigned, "excluded": excluded}


def _first_reason(outcome, windows, embargo) -> str:
    for window in windows:
        if window.contains(outcome.opened_at):
            return assign_to_window(outcome, window, embargo)[1]
    return "opened outside every window"


def independent_sessions(outcomes, gap: timedelta = timedelta(hours=8)) -> int:
    """A count of independent trading sessions, not of tickets.

    Twenty tickets in one basket are one correlated observation, and several
    baskets inside one session are not independent either. Sessions are
    separated by a quiet gap; this is a crude proxy and is labelled as such
    wherever it is reported.
    """
    times = sorted(o.opened_at for o in outcomes)
    if not times:
        return 0
    sessions = 1
    for previous, current in zip(times, times[1:]):
        if current - previous > gap:
            sessions += 1
    return sessions
