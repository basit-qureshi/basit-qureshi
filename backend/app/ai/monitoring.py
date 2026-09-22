"""Drift rules, declared BEFORE any final result is looked at.

The reason this file exists separately from the evaluation code: a threshold
chosen after seeing which threshold would have fired is not a monitoring rule,
it is a description of the past. These are written down first, with the numbers
in them, so a later alert means something.

What an alert does: triggers review, or makes the predictor abstain. What it
never does: increase leverage, widen a loss limit, or quietly change a risk
setting. Nothing in this module can write a setting — it returns findings.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

#: Declared thresholds. Changing one is a deliberate act with a date on it.
RULES = {
    "feature_mean_shift_sigmas": 2.0,       # |live mean - train mean| / train sd
    "missing_feature_rate": 0.10,           # share of predictions with a hole
    "abstention_rate": 0.50,                # share of opportunities abstained
    "prediction_latency_p95_ms": 50.0,      # over the predictor's own budget
    "outcome_mean_error": 10.0,             # |mean predicted net - mean realized|
    "min_samples_before_alerting": 50,      # below this, report but do not alert
}


@dataclass
class Finding:
    rule: str
    triggered: bool
    observed: float | None
    threshold: float
    detail: str
    samples: int

    def as_dict(self) -> dict:
        return {"rule": self.rule, "triggered": self.triggered,
                "observed": self.observed, "threshold": self.threshold,
                "detail": self.detail, "samples": self.samples}


@dataclass
class DriftReport:
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    findings: list = field(default_factory=list)

    @property
    def any_triggered(self) -> bool:
        return any(f.triggered for f in self.findings)

    @property
    def action(self) -> str:
        """The only two outcomes. Neither one touches a risk limit."""
        return "review_or_abstain" if self.any_triggered else "continue"

    def as_dict(self) -> dict:
        return {
            "generated_at": self.generated_at.isoformat(),
            "action": self.action,
            "any_triggered": self.any_triggered,
            "findings": [f.as_dict() for f in self.findings],
            "note": ("An alert triggers review or abstention. It never raises "
                     "exposure and never alters a configured risk limit."),
        }


def check_feature_drift(live_values, train_mean: float, train_sd: float,
                        feature: str) -> Finding:
    values = [v for v in live_values if v is not None]
    n = len(values)
    if n < RULES["min_samples_before_alerting"] or not train_sd:
        return Finding(f"feature_drift:{feature}", False, None,
                       RULES["feature_mean_shift_sigmas"],
                       f"only {n} samples — reported, not alerted", n)
    shift = abs(statistics.fmean(values) - train_mean) / train_sd
    return Finding(
        f"feature_drift:{feature}", shift > RULES["feature_mean_shift_sigmas"],
        round(shift, 3), RULES["feature_mean_shift_sigmas"],
        f"live mean is {shift:.2f} training standard deviations from the training mean", n,
    )


def check_rate(name: str, count: int, total: int, threshold_key: str) -> Finding:
    threshold = RULES[threshold_key]
    if total < RULES["min_samples_before_alerting"]:
        return Finding(name, False, None, threshold,
                       f"only {total} samples — reported, not alerted", total)
    rate = count / total
    return Finding(name, rate > threshold, round(rate, 3), threshold,
                   f"{count} of {total}", total)


def check_latency(latencies) -> Finding:
    values = sorted(v for v in latencies if v is not None)
    n = len(values)
    threshold = RULES["prediction_latency_p95_ms"]
    if n < RULES["min_samples_before_alerting"]:
        return Finding("prediction_latency_p95", False, None, threshold,
                       f"only {n} samples — reported, not alerted", n)
    p95 = values[max(0, min(n - 1, int(round(0.95 * n)) - 1))]
    return Finding("prediction_latency_p95", p95 > threshold, round(p95, 2), threshold,
                   "95th percentile prediction time", n)


def check_outcome_drift(predicted, realized) -> Finding:
    """Only over MATURE outcomes. An unresolved basket has no realized value,
    and pairing a prediction with a zero would manufacture an error of zero."""
    pairs = [(p, r) for p, r in zip(predicted, realized) if p is not None and r is not None]
    n = len(pairs)
    threshold = RULES["outcome_mean_error"]
    if n < RULES["min_samples_before_alerting"]:
        return Finding("outcome_drift", False, None, threshold,
                       f"only {n} matured outcomes — reported, not alerted", n)
    error = abs(statistics.fmean([p for p, _ in pairs]) - statistics.fmean([r for _, r in pairs]))
    return Finding("outcome_drift", error > threshold, round(error, 2), threshold,
                   "mean predicted net versus mean realized net", n)
