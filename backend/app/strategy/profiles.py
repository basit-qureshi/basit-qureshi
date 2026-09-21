"""Named, versioned strategy profiles.

The production profile is `baseline-v1` and it is the strategy exactly as it
runs today: 10 buy stops, 10 sell stops, fixed lot, configured spacing, fixed
combined basket target, no entry filters, no trailing. Phase C does not change
it and does not change grid geometry anywhere.

Every research profile ships with `live_enabled = False`. Offline evidence and
demo operational verification are separate things from approval to trade, and
a flag that defaults to on would collapse the three.

Policy is PINNED TO THE BASKET at admission. Switching the selected profile
while a basket is open must not silently rewrite that basket's exit rules —
the basket keeps the policy it was admitted under, and only global protective
overrides (capital stops, halts, close intents) outrank it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from app.strategy.gates import EventBlackoutGate, ExecutionQualityGate, RegimeGate
from app.strategy.trailing import TrailingPolicy


@dataclass(frozen=True)
class Profile:
    name: str
    version: str
    description: str
    gate_factories: tuple = ()
    trailing: TrailingPolicy | None = None
    #: Research profiles are not eligible for live trading. Only the baseline
    #: is, and only because it is what already runs.
    live_enabled: bool = False
    #: What an UNKNOWN gate verdict means for this profile. Blocking is the
    #: default: a profile that cannot evaluate its own gate has not been shown
    #: to be safe to trade.
    unknown_blocks: bool = True

    @property
    def key(self) -> str:
        return f"{self.name}@{self.version}"

    def build_gates(self) -> list:
        return [factory() for factory in self.gate_factories]

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "key": self.key,
            "description": self.description,
            "gates": [f().name for f in self.gate_factories],
            "trailing": None if self.trailing is None else {
                "version": self.trailing.version,
                "arm_at_usd": self.trailing.arm_at_usd,
                "giveback_usd": self.trailing.giveback_usd,
                "floor_usd": self.trailing.floor_usd,
            },
            "live_enabled": self.live_enabled,
            "unknown_blocks": self.unknown_blocks,
        }


# --- the production profile, unchanged --------------------------------------

BASELINE = Profile(
    name="baseline",
    version="v1",
    description=(
        "The strategy as it runs today: symmetric stop-order straddle, fixed lot, "
        "fixed combined basket target, no entry filters, no trailing exit."
    ),
    gate_factories=(),
    trailing=None,
    live_enabled=True,
)


# --- research candidates, all disabled for live operation --------------------

EXECUTION_QUALITY = Profile(
    name="research-execution-quality",
    version="v1",
    description=(
        "Candidate 1. Admits a basket only on a fresh, valid, reasonably priced "
        "quote. Tests whether expensive or unreliable entry conditions explain "
        "a meaningful share of the losses."
    ),
    gate_factories=(ExecutionQualityGate,),
)

REGIME = Profile(
    name="research-regime",
    version="v1",
    description=(
        "Candidate 2. Admits only when recent closed-bar ATR is large enough "
        "relative to the grid spacing for a breakout straddle to pay. Tests the "
        "claim that this structure needs movement, not a range."
    ),
    gate_factories=(RegimeGate,),
)

EVENT_BLACKOUT = Profile(
    name="research-event-blackout",
    version="v1",
    description=(
        "Candidate 3. Blocks admission around scheduled high-impact releases, "
        "using only schedule entries that were published before the decision. "
        "Blocks entirely while no calendar is loaded."
    ),
    gate_factories=(EventBlackoutGate,),
)

# Candidate 4 is an EXIT change and carries no entry gates, so it is compared
# against the baseline separately rather than mixed into the entry results.
TRAILING = Profile(
    name="research-trailing-exit",
    version="v1",
    description=(
        "Candidate 4. Keeps the fixed target and adds a giveback exit that arms "
        "strictly above it. Capital stops remain authoritative."
    ),
    gate_factories=(),
    trailing=TrailingPolicy(arm_at_usd=15.0, giveback_usd=5.0, floor_usd=10.0),
)

# The one combination worth testing, and only after the individuals are in:
# execution quality plus regime. Both are cheap, causal and independent of a
# data source this repository does not yet have. The event gate is deliberately
# left out of the combination while no calendar exists, because it would block
# every admission and the combination would measure nothing.
EXECUTION_AND_REGIME = Profile(
    name="research-execution-and-regime",
    version="v1",
    description=(
        "Candidates 1 + 2 together. Tested only after both are measured alone, "
        "so a combined result cannot be mistaken for either one's effect."
    ),
    gate_factories=(ExecutionQualityGate, RegimeGate),
)


ALL_PROFILES = {
    p.key: p for p in (BASELINE, EXECUTION_QUALITY, REGIME, EVENT_BLACKOUT,
                       TRAILING, EXECUTION_AND_REGIME)
}


def get_profile(key: str) -> Profile:
    if key not in ALL_PROFILES:
        raise KeyError(f"unknown profile {key!r}; known: {sorted(ALL_PROFILES)}")
    return ALL_PROFILES[key]


def live_profiles() -> list[Profile]:
    return [p for p in ALL_PROFILES.values() if p.live_enabled]
