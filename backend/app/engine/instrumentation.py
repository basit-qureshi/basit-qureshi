"""Timing the execution path honestly.

Three rules this module exists to enforce, because getting any of them wrong
produces a number that looks like evidence and is not:

1. **Elapsed time inside this process comes from a monotonic clock.** Wall
   clocks step when NTP corrects them, and a negative duration or a sudden
   +1000 ms is indistinguishable from a real stall if you measure that way.

2. **Broker event times and local observation times are never subtracted from
   each other.** The terminal's clock and this machine's clock are not
   synchronised, so their difference is an unknown offset plus an unknown
   delay. Calling that difference "network latency" would be inventing a
   measurement. Quote AGE is therefore reported as what it is — a difference
   between two clocks — and explicitly flagged as not a latency figure.

3. **Slow and failed samples are kept.** Dropping a timed-out call because it
   has no clean duration is how a p99 comes out reassuring. A censored
   observation records that it ran for at least N ms and did not finish.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Iterable


def monotonic_ms() -> float:
    return time.monotonic() * 1000.0


def new_correlation_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


@dataclass
class Sample:
    """One measured interval.

    `censored` means the operation did not complete: `ms` is then a LOWER
    BOUND, not a duration. Percentiles report censored samples separately so a
    stall cannot hide inside an otherwise healthy distribution.
    """

    name: str
    ms: float
    censored: bool = False
    failed: bool = False
    correlation_id: str | None = None


@dataclass
class Stats:
    name: str
    count: int = 0
    censored: int = 0
    failed: int = 0
    median: float | None = None
    p95: float | None = None
    p99: float | None = None
    maximum: float | None = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "count": self.count,
            "censored": self.censored,
            "failed": self.failed,
            "median_ms": self.median,
            "p95_ms": self.p95,
            "p99_ms": self.p99,
            "max_ms": self.maximum,
        }


def _percentile(ordered: list[float], fraction: float) -> float:
    """Nearest-rank percentile. Returns None-free values only for non-empty
    input; the caller checks the sample count first."""
    if not ordered:
        raise ValueError("no samples")
    rank = max(1, min(len(ordered), int(round(fraction * len(ordered) + 0.5))))
    return ordered[rank - 1]


def summarize(name: str, samples: Iterable[Sample]) -> Stats:
    """Percentiles over completed samples, with censored and failed counted.

    A percentile is only reported when there are enough samples to support it:
    a "p99" over 12 observations is the maximum wearing a different label.
    """
    samples = list(samples)
    stats = Stats(name=name, count=len(samples))
    stats.censored = sum(1 for s in samples if s.censored)
    stats.failed = sum(1 for s in samples if s.failed)
    # Censored samples still bound the distribution from below, so their lower
    # bound is included rather than discarded.
    values = sorted(s.ms for s in samples)
    if not values:
        return stats
    stats.maximum = round(values[-1], 3)
    stats.median = round(_percentile(values, 0.50), 3)
    if len(values) >= 20:
        stats.p95 = round(_percentile(values, 0.95), 3)
    if len(values) >= 100:
        stats.p99 = round(_percentile(values, 0.99), 3)
    return stats


class Recorder:
    """Collects samples per name, bounded so a long run cannot grow without
    limit. When the cap is hit the OLDEST sample is dropped, never the slowest:
    trimming by value would bias every percentile downward."""

    def __init__(self, cap: int = 5000):
        self.cap = cap
        self._samples: dict[str, list[Sample]] = {}

    def add(self, sample: Sample) -> None:
        bucket = self._samples.setdefault(sample.name, [])
        bucket.append(sample)
        if len(bucket) > self.cap:
            del bucket[0]

    def record(self, name: str, ms: float, *, censored: bool = False,
               failed: bool = False, correlation_id: str | None = None) -> None:
        self.add(Sample(name=name, ms=ms, censored=censored, failed=failed,
                        correlation_id=correlation_id))

    def stats(self, name: str) -> Stats:
        return summarize(name, self._samples.get(name, []))

    def report(self) -> dict:
        return {name: self.stats(name).as_dict() for name in sorted(self._samples)}

    def reset(self) -> None:
        self._samples.clear()

    def span(self, name: str, correlation_id: str | None = None) -> "Span":
        return Span(self, name, correlation_id)


class Span:
    """Times a block. An exception inside marks the sample failed rather than
    losing it, so a path that mostly throws does not look fast."""

    def __init__(self, recorder: Recorder, name: str, correlation_id: str | None = None):
        self.recorder = recorder
        self.name = name
        self.correlation_id = correlation_id
        self.started_ms = 0.0

    def __enter__(self) -> "Span":
        self.started_ms = monotonic_ms()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.recorder.record(
            self.name, monotonic_ms() - self.started_ms,
            failed=exc_type is not None, correlation_id=self.correlation_id,
        )
        return False


@dataclass
class QuoteObservation:
    """A price reading, with the two clocks kept apart.

    `broker_time` is the terminal's stamp on the tick. `observed_monotonic_ms`
    is this process's monotonic reading when it arrived. The gap between the
    broker stamp and local wall time is reported as `age_seconds_two_clock`
    and is NOT a latency measurement — it contains an unknown clock offset.
    Freshness decisions use `observed_monotonic_ms`, which is the only one of
    the two that can be differenced safely.
    """

    price: float | None
    broker_time: object | None
    observed_monotonic_ms: float = field(default_factory=monotonic_ms)
    age_seconds_two_clock: float | None = None
    # MT5's symbol_info_tick returns the LAST tick, not a stream, and can
    # return None. A missing quote is its own state, not a zero and not a
    # reason to reuse a stale one silently.
    missing: bool = False

    def local_age_ms(self, now_ms: float | None = None) -> float:
        return (now_ms if now_ms is not None else monotonic_ms()) - self.observed_monotonic_ms

    def is_stale(self, limit_ms: float, now_ms: float | None = None) -> bool:
        return self.missing or self.local_age_ms(now_ms) > limit_ms
