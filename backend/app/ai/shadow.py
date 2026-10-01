"""Shadow mode: record what a model would have said, change nothing.

The temptation shadow mode exists to resist is counting imaginary money. If a
model says "skip this basket" and the baseline takes it and loses, it is very
easy to write down that the model "saved" that loss. It did not save anything —
it was not trading. The saving is a COUNTERFACTUAL, and a counterfactual is
only as good as the replay that produced it.

So this recorder keeps two separate ledgers:

* **Observed** — the basket the baseline actually took, with the result the
  broker actually reported. This is fact.
* **Counterfactual** — what the model's decision implies, marked `simulated`
  and never added to an observed total.

And it compares against **all baseline opportunities**, not only the ones the
model liked. Scoring a predictor on its favourites is how every strategy looks
profitable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class ShadowRecord:
    """One decision opportunity, as seen by the baseline and by the model."""

    decision_time: datetime
    basket_id: str | None
    prediction: dict
    baseline_admitted: bool
    model_would_admit: bool | None
    #: Filled in later, once the baseline's basket actually resolved. None
    #: while the outcome is immature.
    observed_net: float | None = None
    observed_source: str = "unobserved"
    #: Present only when a replay produced it, and labelled as such.
    simulated_net: float | None = None

    def as_dict(self) -> dict:
        return {
            "decision_time": self.decision_time.isoformat(),
            "basket_id": self.basket_id,
            "prediction": self.prediction,
            "baseline_admitted": self.baseline_admitted,
            "model_would_admit": self.model_would_admit,
            "observed_net": self.observed_net,
            "observed_source": self.observed_source,
            "simulated_net": self.simulated_net,
        }


class ShadowRecorder:
    """Bounded, append-only, and incapable of claiming a skipped profit."""

    def __init__(self, capacity: int = 5000):
        self.capacity = capacity
        self.records: list[ShadowRecord] = []
        self.dropped = 0

    def record(self, *, decision_time, prediction, baseline_admitted: bool,
               model_would_admit: bool | None, basket_id: str | None = None) -> ShadowRecord:
        entry = ShadowRecord(
            decision_time=decision_time, basket_id=basket_id,
            prediction=prediction, baseline_admitted=baseline_admitted,
            model_would_admit=model_would_admit,
        )
        self.records.append(entry)
        if len(self.records) > self.capacity:
            # Oldest out, so a long run cannot grow without bound. Dropping is
            # counted, because a silently truncated ledger is a misleading one.
            del self.records[0]
            self.dropped += 1
        return entry

    def attach_observed_outcome(self, basket_id: str, net: float) -> bool:
        """Links a REAL result to the record that predicted it.

        Only the baseline's own executed baskets can receive one, because only
        those were actually exposed. Everything else stays `unobserved`.
        """
        for entry in self.records:
            if entry.basket_id == basket_id and entry.baseline_admitted:
                entry.observed_net = net
                entry.observed_source = "observed_broker"
                return True
        return False

    def attach_simulated_outcome(self, basket_id: str, net: float) -> bool:
        for entry in self.records:
            if entry.basket_id == basket_id:
                entry.simulated_net = net
                return True
        return False

    def summary(self) -> dict:
        """Coverage and agreement — deliberately NOT a profit comparison.

        There is no "model P&L" here. Producing one would require pricing
        baskets the model declined and the account never held, and that number
        is simulated by definition. It belongs to the replay report, labelled
        as such, not to a ledger that sits beside observed results.
        """
        total = len(self.records)
        consulted = [r for r in self.records if r.model_would_admit is not None]
        agreed = [r for r in consulted if r.model_would_admit == r.baseline_admitted]
        observed = [r for r in self.records if r.observed_net is not None]
        abstained = [r for r in self.records if r.prediction.get("abstained")]

        return {
            "opportunities_recorded": total,
            "dropped_for_capacity": self.dropped,
            "model_consulted": len(consulted),
            "model_abstained": len(abstained),
            "abstention_rate": round(len(abstained) / total, 3) if total else None,
            "coverage": round(len(consulted) / total, 3) if total else None,
            "agreement_with_baseline": round(len(agreed) / len(consulted), 3) if consulted else None,
            "observed_outcomes_linked": len(observed),
            "observed_net_total": round(sum(r.observed_net for r in observed), 2) if observed else None,
            "note": (
                "observed_net_total covers ONLY baskets the baseline actually held. "
                "No profit is attributed to baskets the model would have skipped: "
                "that figure would be counterfactual, and counterfactuals belong to "
                "the replay report where their fill assumptions are stated."
            ),
        }
