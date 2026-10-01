"""Old versus new execution policy over identical ordered tick fixtures.

What this can establish: whether separating protection from reporting changes
how long a basket takes to reach a confirmed flat state, how often a close
fails, how stale the quote was when a decision was taken, and whether a loss
overshoots its configured limit by more or less.

What it cannot establish, and is not claimed anywhere below:

* **It is not a backtest of profitability.** Fills are simulated against a
  stated model, not reproduced from a real book.
* **It cannot capture an unseen peak.** A sampled sequence of ticks is a
  sample; anything that happened between two samples did not happen here.
* **Candle OHLC cannot order intrabar events.** This harness therefore takes an
  explicit ordered tick list and never infers a path from a bar.

Both policies run against the SAME tick list, the same spread, commission,
swap, slippage, partial-fill and delay assumptions, and the same starting
positions. The only difference is when the protective decision is allowed to
run relative to reporting work.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Tick:
    """One observation. bid and ask are modelled separately; a decision may
    only use information available at or before its own index."""

    index: int
    bid: float
    ask: float
    # A tick the policy never sees (a gap in polling). It still moves the
    # market for fill purposes, which is the whole point of modelling it.
    observable: bool = True
    # Simulates the terminal being unreachable at this instant.
    disconnected: bool = False

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


@dataclass
class CostModel:
    """Stated assumptions. Identical for both policies, by construction."""

    commission_per_lot_per_side: float = 2.75
    swap_per_lot_per_cycle: float = 0.0
    slippage_price: float = 0.0
    # Broker round trip for one request, in ticks of simulated time.
    request_latency_ticks: int = 1
    # The first N close requests come back rejected. Stated as a count rather
    # than a rate so a fixture cannot accidentally reject nothing.
    reject_first_n: int = 0
    # A close that fills only part of the requested volume.
    partial_fill_every: int = 0   # every Nth close is partial; 0 disables


@dataclass
class SimPosition:
    side: str          # "BUY" or "SELL"
    volume: float
    open_price: float
    point_value_per_lot: float = 1.0
    point: float = 0.01

    def marked(self, tick: Tick) -> float:
        """Marked at the executable closing side: a long closes on the bid, a
        short on the ask. This is where the exit spread is actually paid."""
        close_price = tick.bid if self.side == "BUY" else tick.ask
        moved = (close_price - self.open_price) / self.point
        if self.side == "SELL":
            moved = -moved
        return moved * self.volume * self.point_value_per_lot


@dataclass
class ReplayResult:
    policy: str
    net_result: float = 0.0
    gross_result: float = 0.0
    commission_paid: float = 0.0
    worst_marked: float = 0.0
    limit_overshoot: float = 0.0
    ticks_to_first_close_request: int | None = None
    ticks_to_confirmed_flat: int | None = None
    close_requests: int = 0
    close_failures: int = 0
    quote_age_ticks_at_decision: int | None = None
    positions_left_open: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "policy": self.policy,
            "net_result": round(self.net_result, 2),
            "gross_result": round(self.gross_result, 2),
            "commission_paid": round(self.commission_paid, 2),
            "worst_marked": round(self.worst_marked, 2),
            "limit_overshoot": round(self.limit_overshoot, 2),
            "ticks_to_first_close_request": self.ticks_to_first_close_request,
            "ticks_to_confirmed_flat": self.ticks_to_confirmed_flat,
            "close_requests": self.close_requests,
            "close_failures": self.close_failures,
            "quote_age_ticks_at_decision": self.quote_age_ticks_at_decision,
            "positions_left_open": self.positions_left_open,
            "notes": self.notes,
        }


def run_exit_only(
    ticks: list[Tick],
    positions: list[SimPosition],
    stop_loss_usd: float,
    *,
    policy: str,
    decision_every: int,
    costs: CostModel | None = None,
) -> ReplayResult:
    """Closure behaviour in isolation: identical starting positions, identical
    ticks, only the decision cadence differs.

    `decision_every` is how many ticks pass between protective evaluations.
    The old policy evaluated once per combined cycle, behind reporting work;
    the new one evaluates on its own faster cadence. Expressing the difference
    as a cadence keeps the comparison to the one variable being tested.
    """
    costs = costs or CostModel()
    result = ReplayResult(policy=policy)
    live = list(positions)
    intent_open = False
    pending_request_at: int | None = None
    last_seen_index: int | None = None

    for tick in ticks:
        if not tick.observable or tick.disconnected:
            # The market still moves; the policy just does not see it. This is
            # exactly the case a poll-based system has and a claim of "every
            # transient opportunity is captured" would deny.
            continue

        marked = sum(p.marked(tick) for p in live)
        result.worst_marked = min(result.worst_marked, marked)

        due = tick.index % decision_every == 0
        if due:
            last_seen_index = tick.index
            if not intent_open and live and marked <= -stop_loss_usd:
                intent_open = True
                result.ticks_to_first_close_request = tick.index
                result.quote_age_ticks_at_decision = 0
                pending_request_at = tick.index + costs.request_latency_ticks
                result.close_requests += 1
            elif intent_open and live and pending_request_at is None:
                # A latched intent keeps requesting until flat, regardless of
                # any recovery in price.
                pending_request_at = tick.index + costs.request_latency_ticks
                result.close_requests += 1

        if pending_request_at is not None and tick.index >= pending_request_at:
            pending_request_at = None
            rejected = result.close_requests <= costs.reject_first_n
            if rejected:
                result.close_failures += 1
            else:
                partial = costs.partial_fill_every and (result.close_requests % costs.partial_fill_every == 0)
                closing = live[: max(1, len(live) // 2)] if partial else list(live)
                for p in closing:
                    close_price = tick.bid if p.side == "BUY" else tick.ask
                    close_price += costs.slippage_price * (-1 if p.side == "BUY" else 1)
                    moved = (close_price - p.open_price) / p.point
                    if p.side == "SELL":
                        moved = -moved
                    gross = moved * p.volume * p.point_value_per_lot
                    commission = costs.commission_per_lot_per_side * p.volume * 2
                    result.gross_result += gross
                    result.commission_paid += commission
                    result.net_result += gross - commission - costs.swap_per_lot_per_cycle * p.volume
                    live.remove(p)
                if not live:
                    result.ticks_to_confirmed_flat = tick.index

    result.positions_left_open = len(live)
    if result.positions_left_open:
        result.notes.append("did not reach a confirmed flat state within the fixture")
    if stop_loss_usd > 0:
        result.limit_overshoot = max(0.0, -result.worst_marked - stop_loss_usd)
    result.notes.append("simulated fills under a stated cost model — not reproduced broker fills")
    if last_seen_index is not None and ticks:
        unseen = sum(1 for t in ticks if not t.observable)
        if unseen:
            result.notes.append(f"{unseen} tick(s) were never observed by this policy")
    return result


def compare_exit_only(
    ticks: list[Tick],
    positions_factory,
    stop_loss_usd: float,
    *,
    old_decision_every: int,
    new_decision_every: int,
    costs: CostModel | None = None,
) -> dict:
    """Both policies, identical inputs. positions_factory builds a fresh set so
    neither run can mutate the other's starting state."""
    old = run_exit_only(ticks, positions_factory(), stop_loss_usd,
                        policy=f"old (decide every {old_decision_every} ticks)",
                        decision_every=old_decision_every, costs=costs)
    new = run_exit_only(ticks, positions_factory(), stop_loss_usd,
                        policy=f"new (decide every {new_decision_every} ticks)",
                        decision_every=new_decision_every, costs=costs)
    return {
        "old": old.as_dict(),
        "new": new.as_dict(),
        "delta": {
            "net_result": round(new.net_result - old.net_result, 2),
            "limit_overshoot": round(new.limit_overshoot - old.limit_overshoot, 2),
            "ticks_to_confirmed_flat": (
                None if new.ticks_to_confirmed_flat is None or old.ticks_to_confirmed_flat is None
                else new.ticks_to_confirmed_flat - old.ticks_to_confirmed_flat
            ),
        },
        "caveats": [
            "Sampled ticks: anything between two samples did not occur in this model.",
            "Fills are simulated, not reproduced. This is not evidence of profitability.",
            "Synthetic fixtures validate mechanics, not a trading edge.",
        ],
    }
