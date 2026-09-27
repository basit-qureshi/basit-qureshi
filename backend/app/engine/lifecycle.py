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
CAUSE_CAPITAL_FLOOR = "capital_floor"
CAUSE_OWNER = "owner_request"
#: A cleanup run by an active liquidation policy, not a basket ending. Counted
#: separately so repeated cleanups cannot inflate the stopped-basket tally.
CAUSE_LIQUIDATION = "liquidation_policy"

# Causes that latch: after these, no new basket may start until the owner acts.
# A loss exit that silently rebuilt would turn one breach into a series of them.
LATCHING_CAUSES = frozenset({CAUSE_BASKET_STOP, CAUSE_DAILY_LOSS, CAUSE_DRAWDOWN,
                             CAUSE_CAPITAL_FLOOR, CAUSE_OWNER, CAUSE_LIQUIDATION})

#: Causes that leave a DURABLE liquidation policy behind once their close
#: confirms. A completed close attempt is not the end of the decision: while one
#: of these is in force, exposure this bot owns that turns up afterwards is
#: cancelled and closed again, not merely managed. The policy is cleared by an
#: explicit owner resume, never by a successful close.
#:
#: A profit exit is deliberately absent: it is not a loss stop, and it permits a
#: replacement. An ordinary pause is absent too — it stops new entries and keeps
#: managing what is open, which is a different instruction.
LIQUIDATING_CAUSES = frozenset({CAUSE_BASKET_STOP, CAUSE_DAILY_LOSS, CAUSE_DRAWDOWN,
                                CAUSE_CAPITAL_FLOOR, CAUSE_OWNER})

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
    #: False for a cleanup ordered by a liquidation policy. Such a close ends no
    #: basket — the basket already ended — so counting it would inflate the
    #: stopped tally once per late fill.
    counts_basket: bool = True
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
            "counts_basket": self.counts_basket,
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


@dataclass
class LiquidationPolicy:
    """A standing instruction to hold this bot's exposure at zero.

    The distinction this exists to make: a close INTENT is one attempt to flatten
    what is open, and it finishes. A liquidation POLICY is the decision that
    followed a loss limit, and it does not finish when a close succeeds. While it
    is in force, any exposure this bot owns that appears afterwards - a late fill,
    an order that survived cancellation, anything carrying its magic number - is
    cancelled and closed again.

    It is cleared by an explicit owner resume and by nothing else. Not by a
    successful close, not by a price recovery, not by a new trading day, and not
    by a restart: it is persisted with the rest of the protective state.

    Ownership is unchanged by it. A position belonging to another strategy or
    opened by hand is never touched, whatever the policy says.
    """

    cause: str
    reason: str
    since_utc: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    #: How many times exposure has had to be cleaned up since the policy began.
    #: A number above zero is worth reading: it means fills kept arriving after
    #: the basket was supposed to be finished.
    cleanups: int = 0
    last_error: str | None = None

    def as_dict(self) -> dict:
        return {"cause": self.cause, "reason": self.reason, "since_utc": self.since_utc,
                "cleanups": self.cleanups, "last_error": self.last_error}

    @classmethod
    def from_dict(cls, data) -> "LiquidationPolicy | None":
        if not isinstance(data, dict) or not data.get("cause"):
            return None
        known = {f for f in cls.__dataclass_fields__}
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
