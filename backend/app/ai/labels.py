"""What a basket's outcome IS, for training purposes.

Getting this wrong is worse than getting the model wrong. Four specific traps:

**Next-candle direction is not the target.** The decision is "is this basket
worth starting", and a basket lives across many candles. Training on the next
bar's direction answers a different question and answers it confidently.

**A win probability is not enough.** This strategy's losses can exceed its
wins — a frozen basket sits at about -$37.80 while a good one makes +$10. A
model that is right 80% of the time and wrong expensively loses money. So the
target carries MAGNITUDE (`net`) and DOWNSIDE (`worst_marked`), and the
decision objective uses both.

**An unresolved basket is not a zero.** A basket still open when the horizon
ends has an outcome nobody knows yet. Labelling it 0.00 would teach the model
that the worst cases are harmless, which is precisely backwards. Those rows are
IMMATURE and excluded from training — and counted, so their exclusion is
visible rather than silent.

**Simulated is not observed.** Outcomes produced by replay are labelled
`simulated` and carry the fill assumptions that produced them. They are never
presented as broker profits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum


class OutcomeStatus(str, Enum):
    RESOLVED = "resolved"          # reached an exit inside the horizon
    IMMATURE = "immature"          # horizon ended first; the result is unknown
    TERMINAL_MARKED = "terminal_marked"   # data ended; marked, not closed


class OutcomeSource(str, Enum):
    SIMULATED_REPLAY = "simulated_replay"
    OBSERVED_BROKER = "observed_broker"


@dataclass(frozen=True)
class BasketOutcomeLabel:
    """The supervised target for one decision opportunity."""

    basket_id: str
    opened_at: datetime
    resolved_at: datetime | None
    status: OutcomeStatus
    source: OutcomeSource
    #: Net result after all modelled costs. None when unresolved.
    net: float | None
    #: Worst marked value seen during the basket's life. Always available,
    #: because it is observed whether or not the basket ever closed.
    worst_marked: float
    #: Whether both sides filled and the volumes cancelled — this strategy's
    #: known dead end. Kept as a label of its own so the frequency of the
    #: failure mode can be measured directly.
    froze_hedged: bool = False
    horizon: timedelta | None = None
    fill_assumptions: dict = field(default_factory=dict)

    @property
    def trainable(self) -> bool:
        """Only a resolved outcome may be trained on.

        A terminally marked basket is honest reporting, but its result depends
        on where the data happened to stop, which is not a property of the
        decision that opened it.
        """
        return self.status is OutcomeStatus.RESOLVED and self.net is not None

    def objective(self, downside_weight: float = 1.0) -> float | None:
        """The scalar the model is fitted against.

        Net alone would rate a basket that made $10 after sitting at -$120 the
        same as one that made $10 calmly. Subtracting weighted downside says
        the route matters, not only the destination.
        """
        if self.net is None:
            return None
        return self.net - downside_weight * abs(min(0.0, self.worst_marked))

    def as_dict(self) -> dict:
        return {
            "basket_id": self.basket_id,
            "opened_at": self.opened_at.isoformat(),
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "status": self.status.value,
            "source": self.source.value,
            "net": self.net,
            "worst_marked": self.worst_marked,
            "froze_hedged": self.froze_hedged,
            "trainable": self.trainable,
            "horizon_minutes": round(self.horizon.total_seconds() / 60, 1) if self.horizon else None,
            "fill_assumptions": self.fill_assumptions,
        }


def label_from_replay(basket, *, horizon: timedelta, fill_assumptions: dict) -> BasketOutcomeLabel:
    """Turns a replay BasketRun into a training label.

    Everything produced here is `SIMULATED_REPLAY`. There is no path in this
    module that can mark a replay result as an observed broker profit.
    """
    resolved = basket.closed_at is not None and not basket.still_open
    within_horizon = (
        resolved and (basket.closed_at - basket.opened_at) <= horizon
    )
    if resolved and within_horizon:
        status = OutcomeStatus.RESOLVED
    elif basket.still_open:
        status = OutcomeStatus.TERMINAL_MARKED
    else:
        status = OutcomeStatus.IMMATURE

    return BasketOutcomeLabel(
        basket_id=basket.basket_id,
        opened_at=basket.opened_at,
        resolved_at=basket.closed_at if resolved else None,
        status=status,
        source=OutcomeSource.SIMULATED_REPLAY,
        net=basket.net_result if status is OutcomeStatus.RESOLVED else None,
        worst_marked=basket.worst_net,
        froze_hedged=getattr(basket, "froze_hedged", False),
        horizon=horizon,
        fill_assumptions=dict(fill_assumptions),
    )


def summarize_labels(labels) -> dict:
    """Counts by status, so exclusions are reported rather than hidden."""
    labels = list(labels)
    resolved = [l for l in labels if l.status is OutcomeStatus.RESOLVED]
    return {
        "total": len(labels),
        "resolved": len(resolved),
        "immature_excluded": sum(1 for l in labels if l.status is OutcomeStatus.IMMATURE),
        "terminal_marked_excluded": sum(1 for l in labels if l.status is OutcomeStatus.TERMINAL_MARKED),
        "froze_hedged": sum(1 for l in labels if l.froze_hedged),
        "sources": sorted({l.source.value for l in labels}),
        "mean_net_resolved": (
            round(sum(l.net for l in resolved) / len(resolved), 2) if resolved else None
        ),
        "worst_marked_overall": round(min((l.worst_marked for l in labels), default=0.0), 2),
    }
