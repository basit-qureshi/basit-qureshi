"""Deterministic causal replay of the straddle over bid/ask ticks.

Causality is enforced structurally, not by convention: the loop hands a
decision only the ticks it has already passed. There is no lookahead because
future ticks are not reachable from where a decision is made.

Fill rules, stated plainly:

* A BUY STOP at L fills when the **ask** reaches L. A long is opened at the
  ask and later closed at the bid.
* A SELL STOP at L fills when the **bid** reaches L. A short is opened at the
  bid and closed at the ask.
* A level crossed during an interval the replay never observed does NOT fill at
  that level. If the next observed tick is beyond it, the fill is marked
  `gapped` and priced at the observed tick — which is worse, and honest. There
  are no ideal fills at prices nobody saw.

What this cannot do, and never claims: it cannot reproduce the broker's actual
book, cannot know intrabar order from candle OHLC (so it refuses to run on
bars at all), and cannot capture a peak that existed between two ticks. Open
exposure at the end is marked and reported, never dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass(frozen=True)
class ReplayCosts:
    commission_per_lot_per_side: float = 2.75
    swap_per_lot_per_day: float = 0.0
    #: Extra price paid beyond the quoted side, in price units.
    slippage: float = 0.0
    #: Delay between deciding and the request reaching the book.
    decision_latency: timedelta = timedelta(milliseconds=0)


@dataclass
class ReplayPosition:
    side: str
    volume: float
    open_price: float
    opened_at: datetime
    gapped: bool = False
    point: float = 0.01
    point_value_per_lot: float = 1.0

    def marked(self, bid: float, ask: float) -> float:
        close_price = bid if self.side == "BUY" else ask
        moved = (close_price - self.open_price) / self.point
        if self.side == "SELL":
            moved = -moved
        return moved * self.volume * self.point_value_per_lot


@dataclass
class BasketRun:
    basket_id: str
    opened_at: datetime
    reference: float
    profile_key: str
    closed_at: datetime | None = None
    close_reason: str | None = None
    net_result: float | None = None
    peak_net: float = 0.0
    worst_net: float = 0.0
    fills: int = 0
    gapped_fills: int = 0
    commission: float = 0.0
    still_open: bool = False
    froze_hedged: bool = False

    def as_dict(self) -> dict:
        return {
            "basket_id": self.basket_id,
            "profile": self.profile_key,
            "opened_at": self.opened_at.isoformat(),
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "close_reason": self.close_reason,
            "net_result": None if self.net_result is None else round(self.net_result, 2),
            "peak_net": round(self.peak_net, 2),
            "worst_net": round(self.worst_net, 2),
            "fills": self.fills,
            "gapped_fills": self.gapped_fills,
            "commission": round(self.commission, 2),
            "still_open": self.still_open,
            "froze_hedged": self.froze_hedged,
        }


@dataclass
class ReplayReport:
    profile_key: str
    baskets: list[BasketRun] = field(default_factory=list)
    admissions_blocked: int = 0
    block_reasons: dict = field(default_factory=dict)
    ticks_seen: int = 0
    remaining_exposure_marked: float = 0.0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        closed = [b for b in self.baskets if b.net_result is not None and not b.still_open]
        wins = [b for b in closed if b.net_result > 0]
        losses = [b for b in closed if b.net_result < 0]
        gross_win = sum(b.net_result for b in wins)
        gross_loss = abs(sum(b.net_result for b in losses))
        net = sum(b.net_result for b in closed)
        worst = min((b.worst_net for b in self.baskets), default=0.0)
        return {
            "profile": self.profile_key,
            "baskets_completed": len(closed),
            "baskets_still_open": sum(1 for b in self.baskets if b.still_open),
            "froze_hedged": sum(1 for b in self.baskets if b.froze_hedged),
            "net_result": round(net, 2),
            "result_per_basket": round(net / len(closed), 2) if closed else None,
            "wins": len(wins),
            "losses": len(losses),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "commission_paid": round(sum(b.commission for b in self.baskets), 2),
            "worst_basket_marked": round(worst, 2),
            "gapped_fills": sum(b.gapped_fills for b in self.baskets),
            "admissions_blocked": self.admissions_blocked,
            "block_reasons": self.block_reasons,
            "ticks_seen": self.ticks_seen,
            "remaining_exposure_marked": round(self.remaining_exposure_marked, 2),
            "notes": self.notes,
        }


class StraddleReplay:
    """Replays the production structure: stops above and below a reference.

    Geometry is taken from the live configuration and is NOT changed here.
    Phase C tests admission and exit policy, not the shape of the grid.
    """

    def __init__(self, *, lot: float, buy_levels: int, sell_levels: int, spacing: float,
                 target_usd: float, stop_usd: float, costs: ReplayCosts | None = None,
                 max_open_positions: int = 20):
        self.lot = lot
        self.buy_levels = buy_levels
        self.sell_levels = sell_levels
        self.spacing = spacing
        self.target_usd = target_usd
        self.stop_usd = stop_usd
        self.costs = costs or ReplayCosts()
        self.max_open_positions = max_open_positions

    def _levels(self, reference: float):
        buys = [reference + self.spacing * (i + 1) for i in range(self.buy_levels)]
        sells = [reference - self.spacing * (i + 1) for i in range(self.sell_levels)]
        return buys, sells

    def run(self, ticks, *, profile, admission_fn=None, trailing_factory=None,
            warmup_ticks: int = 0) -> ReplayReport:
        """One pass. `admission_fn(tick, index)` returns an AdmissionDecision.

        `warmup_ticks` is skipped for ADMISSION only — the market still moves
        during warmup, it is simply not traded, which is what a real indicator
        warmup looks like.
        """
        report = ReplayReport(profile_key=profile.key)
        report.notes.append(
            "Simulated fills on observed bid/ask ticks. Not reproduced broker fills."
        )
        basket: BasketRun | None = None
        open_positions: list[ReplayPosition] = []
        pending_buys: list[float] = []
        pending_sells: list[float] = []
        trailing = None
        counter = 0

        for index, tick in enumerate(ticks):
            report.ticks_seen += 1

            if basket is None:
                if index < warmup_ticks:
                    continue
                decision = admission_fn(tick, index) if admission_fn else None
                if decision is not None and not decision.allowed:
                    report.admissions_blocked += 1
                    for reason in decision.blocking_reasons:
                        key = reason.split(":")[0][:60]
                        report.block_reasons[key] = report.block_reasons.get(key, 0) + 1
                    continue
                counter += 1
                basket = BasketRun(
                    basket_id=f"b{counter}", opened_at=tick.time,
                    reference=tick.mid, profile_key=profile.key,
                )
                pending_buys, pending_sells = self._levels(tick.mid)
                open_positions = []
                trailing = trailing_factory(basket.basket_id) if trailing_factory else None
                continue

            # --- fills ------------------------------------------------------
            if len(open_positions) < self.max_open_positions:
                for level in list(pending_buys):
                    if tick.ask >= level:
                        pending_buys.remove(level)
                        gapped = tick.ask > level + 1e-9
                        price = (tick.ask if gapped else level) + self.costs.slippage
                        open_positions.append(ReplayPosition("BUY", self.lot, price, tick.time, gapped))
                        basket.fills += 1
                        basket.gapped_fills += 1 if gapped else 0
                        basket.commission += self.costs.commission_per_lot_per_side * self.lot * 2
                for level in list(pending_sells):
                    if tick.bid <= level:
                        pending_sells.remove(level)
                        gapped = tick.bid < level - 1e-9
                        price = (tick.bid if gapped else level) - self.costs.slippage
                        open_positions.append(ReplayPosition("SELL", self.lot, price, tick.time, gapped))
                        basket.fills += 1
                        basket.gapped_fills += 1 if gapped else 0
                        basket.commission += self.costs.commission_per_lot_per_side * self.lot * 2
            else:
                pending_buys, pending_sells = [], []

            # --- valuation ---------------------------------------------------
            marked = sum(p.marked(tick.bid, tick.ask) for p in open_positions)
            net = marked - basket.commission
            basket.peak_net = max(basket.peak_net, net)
            basket.worst_net = min(basket.worst_net, net)

            buy_vol = sum(p.volume for p in open_positions if p.side == "BUY")
            sell_vol = sum(p.volume for p in open_positions if p.side == "SELL")
            if open_positions and abs(buy_vol - sell_vol) < 1e-9 and not pending_buys and not pending_sells:
                basket.froze_hedged = True

            # --- exits, in the order the live engine applies them -------------
            reason = None
            if self.stop_usd > 0 and net <= -self.stop_usd:
                reason = f"basket stop ({net:.2f})"
            elif trailing is not None:
                trailing.observe(net, costs_known=True)
                should, why = trailing.should_exit(net, costs_known=True)
                if should:
                    reason = why
                elif net >= self.target_usd:
                    reason = f"fixed target ({net:.2f})"
            elif net >= self.target_usd:
                reason = f"fixed target ({net:.2f})"

            if reason:
                basket.closed_at = tick.time
                basket.close_reason = reason
                basket.net_result = net
                report.baskets.append(basket)
                basket, open_positions = None, []
                pending_buys, pending_sells, trailing = [], [], None

        if basket is not None:
            # Marked, not dropped. Dropping it would remove exactly the basket
            # that never recovered.
            last = ticks[-1] if ticks else None
            if last is not None:
                marked = sum(p.marked(last.bid, last.ask) for p in open_positions)
                basket.net_result = marked - basket.commission
                report.remaining_exposure_marked = basket.net_result
            basket.still_open = True
            report.baskets.append(basket)
            report.notes.append(
                "A basket was still open when the data ended; it is marked at the last "
                "observed tick and reported as remaining exposure."
            )
        return report
