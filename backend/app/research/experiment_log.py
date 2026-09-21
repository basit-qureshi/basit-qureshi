"""Every attempt, recorded — and a freeze that makes the final window mean
something.

The failure this prevents: run a candidate against the final evaluation window,
see it fail, adjust a threshold, run again, and report the last attempt as
though the window were untouched. After the second look it is not untouched,
and no amount of calling it a holdout changes that.

So: candidates and parameter sets are logged as they are tried, exactly one
selection is FROZEN, and the final window may be opened once. A second open
against a different candidate is refused, and the refusal is itself recorded.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Attempt:
    candidate: str
    params: dict
    window: str
    metrics: dict
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def as_dict(self) -> dict:
        return {"candidate": self.candidate, "params": self.params,
                "window": self.window, "metrics": self.metrics, "at": self.at}


class FinalWindowAlreadyOpened(RuntimeError):
    """A second candidate was taken to the final window.

    Not a warning. Allowing it is how a holdout stops being one.
    """


class ExperimentLog:
    def __init__(self, budget: int | None = None):
        #: Declared BEFORE looking at any final result. Exceeding it is
        #: recorded rather than silently permitted, because an undeclared
        #: search over many candidates makes any single p-value meaningless.
        self.budget = budget
        self.attempts: list[Attempt] = []
        self.frozen: dict | None = None
        self.final_opened_for: str | None = None
        self.refusals: list[dict] = []

    def record(self, candidate: str, params: dict, window: str, metrics: dict) -> Attempt:
        attempt = Attempt(candidate, params, window, metrics)
        self.attempts.append(attempt)
        return attempt

    @property
    def over_budget(self) -> bool:
        return self.budget is not None and len(self.distinct_candidates) > self.budget

    @property
    def distinct_candidates(self) -> set:
        return {a.candidate for a in self.attempts}

    def freeze(self, candidate: str, params: dict) -> dict:
        """Locks in the selection. Must happen before the final window opens."""
        if self.final_opened_for is not None:
            raise FinalWindowAlreadyOpened(
                f"the final window was already opened for {self.final_opened_for!r}; "
                "freezing another candidate now would be selecting on it"
            )
        payload = {"candidate": candidate, "params": params,
                   "frozen_at": datetime.now(timezone.utc).isoformat()}
        payload["fingerprint"] = hashlib.sha256(
            json.dumps({"candidate": candidate, "params": params}, sort_keys=True).encode()
        ).hexdigest()[:16]
        self.frozen = payload
        return payload

    def open_final(self, candidate: str):
        """Opens the final evaluation window, once, for the frozen candidate."""
        if self.frozen is None:
            raise RuntimeError("nothing was frozen — freeze a selection before opening the final window")
        if candidate != self.frozen["candidate"]:
            refusal = {"requested": candidate, "frozen": self.frozen["candidate"],
                       "at": datetime.now(timezone.utc).isoformat()}
            self.refusals.append(refusal)
            raise FinalWindowAlreadyOpened(
                f"frozen candidate is {self.frozen['candidate']!r}, not {candidate!r}"
            )
        if self.final_opened_for is not None:
            refusal = {"requested": candidate, "already_opened_for": self.final_opened_for,
                       "at": datetime.now(timezone.utc).isoformat()}
            self.refusals.append(refusal)
            raise FinalWindowAlreadyOpened(
                f"the final window has already been opened for {self.final_opened_for!r}"
            )
        self.final_opened_for = candidate
        return self.frozen

    def as_dict(self) -> dict:
        return {
            "declared_budget": self.budget,
            "distinct_candidates_tried": sorted(self.distinct_candidates),
            "over_budget": self.over_budget,
            "attempts": [a.as_dict() for a in self.attempts],
            "frozen": self.frozen,
            "final_opened_for": self.final_opened_for,
            "refusals": self.refusals,
        }
