"""The day's risk reading, as separate and individually defined quantities.

The engine used to judge its daily loss limit on `_day_realized`: an in-memory
float, incremented from whatever `close_position()` happened to return, reset to
zero by a restart, and blind to every open position. Three things were wrong
with that at once. A close return value is not settlement evidence. A restart
handed the account a fresh loss budget. And a basket sitting at -$200 did not
move the number the limit was judged on at all.

This module replaces it. Every quantity below is defined once, sourced once, and
counted once.

Where each fee lives
--------------------
A trade is in exactly one of two places, never both, so no fee is counted twice:

* **Settled** - the broker has reported a final figure. For MT5 that figure is
  the sum over the position's deals of profit + commission + swap + fee, so
  entry commission, exit commission and swap are all already inside it.
* **Open mark** - `Position.net_profit`, which is the broker's floating profit
  plus the swap and commission booked *so far*. The exit commission has not been
  incurred yet and is therefore not in it.

The exit reserve is an estimate of what closing would still cost. It is NOT part
of the historical result and is reported separately, because mixing an estimate
into a settled figure makes the settled figure a guess.

The identity
------------
    marked_result = settled_realized
                  + (open_marked - day_open_marked)
                  + pending_settlement_marked

`day_open_marked` is the anchor: the marked value of whatever this bot was
already holding when the trading day rolled over. Subtracting it is what stops
yesterday's unrealised loss from being charged to today a second time.

`pending_settlement_marked` covers the gap between a position leaving the
broker's open list and its realised figure arriving. Without it the reading
would jump back toward zero for as long as settlement was delayed - the loss
would appear to vanish precisely while it was becoming permanent.
"""

from dataclasses import dataclass, field


def _r(value: float) -> float:
    """Round for storage and display only, never before a comparison."""
    return round(value + 0.0, 2)


@dataclass(frozen=True)
class DayRisk:
    """One trading day's risk picture for one bot identity.

    Scope: this account_id, symbol, magic number and mode. Manual trades and
    other programs are excluded by construction - nothing else is ever queried.
    Deposits, withdrawals and credits are account cashflows, not trading
    results, and are deliberately absent from every field here; they move
    account equity and therefore the drawdown anchor, not the day's result.
    """

    trading_day: str | None = None

    # Trades closed today whose final figure the broker has reported. Includes
    # profit, entry and exit commission, swap and fees.
    settled_realized: float = 0.0

    # Marked value of everything this bot holds right now: floating profit plus
    # swap and commission booked so far. Excludes the not-yet-incurred exit cost.
    open_marked: float = 0.0

    # The anchor. Marked value of positions already held when the day rolled.
    # None means no anchor could be established for this day.
    day_open_marked: float | None = 0.0

    # Last known mark of positions that have left the broker's open list but
    # whose realised figure has not arrived. Keeps the reading stable across
    # the settlement gap instead of letting the loss briefly disappear.
    pending_settlement_marked: float = 0.0

    # Conservative estimate of what closing the current basket would still cost.
    # Reported separately and never folded into the historical result. None when
    # it could not be estimated.
    exit_reserve: float | None = 0.0

    # Count of trades closed today with no realised figure yet.
    unsettled_count: int = 0

    # False when a required anchor or a history segment could not be
    # reconstructed. An incomplete reading may still protect existing exposure,
    # but it must not authorise new exposure.
    complete: bool = True
    incomplete_reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def open_mark_change(self) -> float:
        """How much the open mark has moved since the day's anchor.

        With no anchor this is not a defined quantity, so it reports the whole
        open mark and `complete` is False - the caller must not treat it as a
        trustworthy day delta.
        """
        anchor = self.day_open_marked if self.day_open_marked is not None else 0.0
        return self.open_marked - anchor

    @property
    def marked_result(self) -> float:
        """The day's result including current floating exposure.

        This is the quantity the daily loss limit is judged on. It is not the
        same thing as the realised daily card on the dashboard, and it is not
        supposed to be: with positions open, a realised figure and a marked
        figure are different measurements of different things.
        """
        return self.settled_realized + self.open_mark_change + self.pending_settlement_marked

    @property
    def risk_reading(self) -> float:
        """`marked_result` with the exit reserve applied, when it is known.

        Kept distinct from `marked_result` so the historical part of the number
        is never contaminated by an estimate.
        """
        reserve = self.exit_reserve or 0.0
        return self.marked_result - reserve

    def as_dict(self) -> dict:
        """The wire form. Field names say what they measure, so a card cannot
        silently relabel one quantity as another."""
        return {
            "trading_day": self.trading_day,
            "settled_realized_usd": _r(self.settled_realized),
            "open_marked_usd": _r(self.open_marked),
            "day_open_marked_usd": None if self.day_open_marked is None else _r(self.day_open_marked),
            "open_mark_change_usd": _r(self.open_mark_change),
            "pending_settlement_marked_usd": _r(self.pending_settlement_marked),
            "exit_reserve_usd": None if self.exit_reserve is None else _r(self.exit_reserve),
            "marked_result_usd": _r(self.marked_result),
            "risk_reading_usd": _r(self.risk_reading),
            "unsettled_count": self.unsettled_count,
            "complete": self.complete,
            "incomplete_reasons": list(self.incomplete_reasons),
        }


def build_day_risk(
    *,
    trading_day: str | None,
    settled_realized: float,
    unsettled_count: int,
    open_positions_marked: float,
    day_open_marked: float | None,
    pending_settlement_marked: float = 0.0,
    exit_reserve: float | None = 0.0,
    extra_incomplete: tuple[str, ...] = (),
) -> DayRisk:
    """Assembles a DayRisk and decides whether it may authorise new exposure.

    A reading is incomplete - and therefore not a basis for opening anything
    new - when the day is unknown, when the opening anchor is missing while
    exposure exists, or when the caller reports a gap of its own.
    """
    reasons: list[str] = list(extra_incomplete)

    if trading_day is None:
        reasons.append("the broker trading day could not be determined")
    if day_open_marked is None:
        reasons.append(
            "no opening exposure anchor for this day, so today's share of the open mark is unknown"
        )
    if unsettled_count:
        # Not fatal on its own: the pending mark below keeps the reading honest.
        # It is surfaced so the owner can see the figure is still moving.
        reasons.append(f"{unsettled_count} trade(s) closed today have no settled figure yet")

    return DayRisk(
        trading_day=trading_day,
        settled_realized=settled_realized,
        open_marked=open_positions_marked,
        day_open_marked=day_open_marked,
        pending_settlement_marked=pending_settlement_marked,
        exit_reserve=exit_reserve,
        unsettled_count=unsettled_count,
        # An unsettled trade does not make the reading unusable, because its
        # last known mark is still being carried. A missing day or anchor does.
        complete=trading_day is not None and day_open_marked is not None and not extra_incomplete,
        incomplete_reasons=tuple(reasons),
    )
