"""The close intent, and what the bot is allowed to do while one exists.

Before this, a basket stop called `_close_everything` once and moved on. If the
broker rejected those closes and the price then recovered above the stop level,
the condition that fired was no longer true, so nothing tried again - and the
UI had already been handed a hardcoded empty position list. The exposure was
live, unwatched, and invisible.

A close intent fixes that by being a *decision that was taken*, not a condition
that happens to be true right now. Once it exists, price recovery is irrelevant:
it is driven to a broker-confirmed flat state and only then retired.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone


# Why the close was ordered. The cause decides what happens afterwards, which is
# not the same question as whether the close succeeded.
CAUSE_PROFIT = "profit_target"
CAUSE_BASKET_STOP = "basket_stop"
CAUSE_DAILY_LOSS = "daily_loss"
CAUSE_DRAWDOWN = "equity_drawdown"
CAUSE_OWNER = "owner_request"

# Causes that latch: after these, no new basket may start until the owner acts.
# A loss exit that silently rebuilt would turn one breach into a series of them.
LATCHING_CAUSES = frozenset({CAUSE_BASKET_STOP, CAUSE_DAILY_LOSS, CAUSE_DRAWDOWN, CAUSE_OWNER})

STATE_CLOSING = "CLOSING"          # cancelling orders and closing positions
STATE_RECONCILING = "RECONCILING"  # closes attempted, confirming nothing survived
STATE_DONE = "DONE"                # broker confirmed flat


@dataclass
class CloseIntent:
    """A decision to flatten this bot's exposure, and its progress.

    `reason` is the human sentence. `cause` is the machine category. They are
    kept apart so the original cause survives even when the final broker
    outcome is something else entirely.
    """

    cause: str
    reason: str
    basket_id: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    state: str = STATE_CLOSING
    attempts: int = 0
    # Set once the basket has been counted in the won/stopped tallies, so a
    # retry loop cannot count the same basket several times.
    counted: bool = False
    # Final realised figure, filled in when settlement arrives. None until then;
    # never zero as a placeholder.
    realized_net: float | None = None
    last_error: str | None = None

    @property
    def latches_entries(self) -> bool:
        return self.cause in LATCHING_CAUSES

    @property
    def finished(self) -> bool:
        return self.state == STATE_DONE

    def as_dict(self) -> dict:
        return {
            "cause": self.cause,
            "reason": self.reason,
            "basket_id": self.basket_id,
            "created_at": self.created_at,
            "state": self.state,
            "attempts": self.attempts,
            "counted": self.counted,
            "realized_net": self.realized_net,
            "last_error": self.last_error,
            "latches_entries": self.latches_entries,
        }

    @classmethod
    def from_dict(cls, data) -> "CloseIntent | None":
        if not isinstance(data, dict) or not data.get("cause"):
            return None
        known = {f for f in cls.__dataclass_fields__ if f != "latches_entries"}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass(frozen=True)
class Admission:
    """The answer to 'may a new basket be created right now'.

    `allowed` is never True on a missing input. Everything that could not be
    established appears in `unknowns`, and an unknown blocks rather than
    defaults to permissive - which is the whole difference between a guard and
    a comment.
    """

    allowed: bool
    reason: str | None = None
    unknowns: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def ok(cls) -> "Admission":
        return cls(allowed=True)

    @classmethod
    def refuse(cls, reason: str, *unknowns: str) -> "Admission":
        return cls(allowed=False, reason=reason, unknowns=tuple(unknowns))
