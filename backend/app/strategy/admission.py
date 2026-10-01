"""One interface for deciding whether a NEW basket may start.

Scope, stated once so it cannot drift: these gates control **admission**. They
never touch an open basket's protection. An active close intent, a loss halt, a
drawdown breach and the capital admission checks all outrank everything here —
a research gate that could delay a close or release a halt would be a way of
losing money with a filter attached.

Three outcomes, not two. `UNKNOWN` exists because "I could not tell" is a real
state and collapsing it into either ALLOW or BLOCK is a choice with a cost:
collapsing to ALLOW trades on absent data, collapsing to BLOCK makes a broken
feed indistinguishable from a genuine veto. Each profile declares what it does
with an unknown, and a gate whose data is required says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class Verdict(str, Enum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class GateResult:
    """One gate's answer, with everything needed to audit it later.

    `inputs_as_of` is when the data the gate looked at was true — not when the
    gate ran. A gate that reports its own run time would hide the fact that it
    decided on a ten-minute-old quote.
    """

    gate: str
    verdict: Verdict
    reason: str
    inputs_as_of: datetime | None = None
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    detail: dict = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.verdict is Verdict.BLOCK

    def as_dict(self) -> dict:
        return {
            "gate": self.gate,
            "verdict": self.verdict.value,
            "reason": self.reason,
            "inputs_as_of": self.inputs_as_of.isoformat() if self.inputs_as_of else None,
            "evaluated_at": self.evaluated_at.isoformat(),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class AdmissionDecision:
    """The combined answer for one profile at one moment."""

    allowed: bool
    profile: str
    profile_version: str
    results: tuple[GateResult, ...] = ()

    @property
    def blocking_reasons(self) -> list[str]:
        return [r.reason for r in self.results if r.verdict is not Verdict.ALLOW]

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "profile": self.profile,
            "profile_version": self.profile_version,
            "gates": [r.as_dict() for r in self.results],
            "blocking_reasons": self.blocking_reasons,
        }


class Gate:
    """A single admission check.

    Implementations get a context dict and return a GateResult. They must be
    pure with respect to the context: no broker calls, no clock reads for
    decision purposes, no I/O. That is what makes the same gate usable in
    runtime and in a causal replay without one behaving differently.
    """

    name = "gate"
    #: When True, an UNKNOWN from this gate blocks. A profile that needs a
    #: calendar it does not have must not trade as though there were no events.
    data_required = False

    def evaluate(self, ctx: dict) -> GateResult:  # pragma: no cover - interface
        raise NotImplementedError

    # helpers
    def allow(self, reason: str, as_of=None, **detail) -> GateResult:
        return GateResult(self.name, Verdict.ALLOW, reason, as_of, detail=detail)

    def block(self, reason: str, as_of=None, **detail) -> GateResult:
        return GateResult(self.name, Verdict.BLOCK, reason, as_of, detail=detail)

    def unknown(self, reason: str, as_of=None, **detail) -> GateResult:
        return GateResult(self.name, Verdict.UNKNOWN, reason, as_of, detail=detail)


def evaluate_all(gates, ctx: dict, profile: str, version: str,
                 unknown_blocks: bool = True) -> AdmissionDecision:
    """Runs every gate and combines them.

    Every gate runs even after one blocks, so the dashboard can show all the
    reasons rather than whichever happened to be checked first. `unknown_blocks`
    is the profile's declared policy for a gate that could not decide; a gate
    marked `data_required` blocks on UNKNOWN regardless.
    """
    results = []
    allowed = True
    for gate in gates:
        try:
            result = gate.evaluate(ctx)
        except Exception as exc:
            result = GateResult(getattr(gate, "name", "gate"), Verdict.UNKNOWN,
                                f"gate raised: {exc}")
        results.append(result)
        if result.verdict is Verdict.BLOCK:
            allowed = False
        elif result.verdict is Verdict.UNKNOWN and (unknown_blocks or gate.data_required):
            allowed = False
    return AdmissionDecision(allowed=allowed, profile=profile,
                             profile_version=version, results=tuple(results))
