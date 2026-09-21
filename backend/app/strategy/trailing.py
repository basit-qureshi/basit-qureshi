"""Candidate 4: an optional trailing exit on the BASKET's net profit.

Hypothesis: the fixed $10 basket target leaves money on the table when a move
keeps running, and a giveback rule that arms above the target captures more of
it without increasing risk.

Falsified if: net result per basket does not improve against the fixed target
over the same data, or if drawdown or time-exposed gets worse.

Precedence, stated once. This is an EXIT ADDITION, never a relaxation:

1. Capital stops — basket stop, daily limit, drawdown — outrank everything.
   Trailing cannot hold a basket open past one of them.
2. The fixed target still fires. Trailing arms strictly ABOVE it, so a basket
   that reaches the target without arming closes exactly as it does today.
3. Once trailing triggers, the resulting close intent is durable. A price
   recovery afterwards does not cancel it, same as every other exit.

Two things it is not. A floor is a *trigger*, not a fill: the price it triggers
at is not the price it gets. And nothing here knows a future peak — it can only
ever give back part of a peak it has already seen.

Unknown costs cannot manufacture a peak. `observe` refuses to raise the peak on
a valuation whose costs were not reported, because a peak inflated by an
unreported commission would set the giveback floor too high and hold a basket
open on money that was never there.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TrailingState:
    """Per-basket trailing state. Persisted, and scoped to basket + account.

    Scoping matters: a peak carried from one basket into the next would arm the
    new basket at a level it never reached, and a peak carried across accounts
    would be someone else's number entirely.
    """

    basket_id: str
    account_id: str
    armed: bool = False
    peak_net: float | None = None
    #: Observations whose costs were unknown, kept so the UI can say the peak
    #: may be understated rather than presenting it as exact.
    skipped_unknown_cost: int = 0

    def as_dict(self) -> dict:
        return {
            "basket_id": self.basket_id,
            "account_id": self.account_id,
            "armed": self.armed,
            "peak_net": self.peak_net,
            "skipped_unknown_cost": self.skipped_unknown_cost,
        }

    @classmethod
    def from_dict(cls, data) -> "TrailingState | None":
        if not isinstance(data, dict) or not data.get("basket_id"):
            return None
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass(frozen=True)
class TrailingPolicy:
    """The rule. Every number is explicit; none of them is a default that
    happens to look good on one sample."""

    #: Trailing arms only once net profit reaches this. Strictly above the
    #: fixed target, so the existing behaviour is untouched below it.
    arm_at_usd: float
    #: How much of the peak may be given back before closing.
    giveback_usd: float
    #: Never exit below this net profit, whatever the giveback says.
    floor_usd: float
    version: str = "trailing-v1"

    def __post_init__(self):
        if self.arm_at_usd <= 0 or self.giveback_usd <= 0:
            raise ValueError("arm_at_usd and giveback_usd must be positive")
        if self.floor_usd > self.arm_at_usd:
            raise ValueError("floor cannot be above the arming threshold")

    def validate_against_target(self, fixed_target_usd: float) -> None:
        """Arming at or below the fixed target would mean the basket closed at
        the target before trailing could ever arm, making the whole candidate a
        no-op that still looked like it was running."""
        if self.arm_at_usd <= fixed_target_usd:
            raise ValueError(
                f"arm_at_usd ({self.arm_at_usd}) must be above the fixed target "
                f"({fixed_target_usd}); otherwise the target always fires first"
            )


class BasketTrailing:
    """Applies a TrailingPolicy to one basket's valuations."""

    def __init__(self, policy: TrailingPolicy, state: TrailingState):
        self.policy = policy
        self.state = state

    def observe(self, net_profit: float, costs_known: bool) -> None:
        """Records a valuation. Does not decide; `should_exit` does that."""
        if not costs_known:
            # A peak set from an incomplete valuation would raise the giveback
            # floor on money that may not exist.
            self.state.skipped_unknown_cost += 1
            return
        if self.state.peak_net is None or net_profit > self.state.peak_net:
            self.state.peak_net = net_profit
        if not self.state.armed and net_profit >= self.policy.arm_at_usd:
            self.state.armed = True

    def should_exit(self, net_profit: float, costs_known: bool) -> tuple[bool, str | None]:
        """Whether the trailing rule says to close now.

        Returns (False, None) when it does not apply. The caller still checks
        the fixed target and every capital stop — this never replaces them.
        """
        if not self.state.armed or self.state.peak_net is None:
            return False, None
        if not costs_known:
            # Refusing to trigger on an unknown valuation is deliberate: the
            # capital stops remain authoritative and are not affected by this.
            return False, None
        threshold = self.state.peak_net - self.policy.giveback_usd
        if threshold < self.policy.floor_usd:
            threshold = self.policy.floor_usd
        if net_profit <= threshold:
            return True, (
                f"trailing exit ({self.policy.version}): net {net_profit:.2f} gave back to "
                f"{threshold:.2f} from a peak of {self.state.peak_net:.2f}"
            )
        return False, None
