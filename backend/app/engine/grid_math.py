"""The completed-grid arithmetic, as a pure function with named inputs.

Why this is its own module. The engine's admission policy and the offline
compatibility report have to produce the SAME number, or the report is
decoration. Both call `completed_grid_estimate`; nothing computes it twice.

What the estimate is, precisely. One scenario: every configured level fills at
exactly its own price, the two sides end with equal volume, the entry spread is
paid once per fill, and the basket is valued as if closed back at the reference
price. In that state the volumes cancel and the marked loss stops responding to
price.

What it is NOT. A maximum loss. It has no exit cost, no commission, no swap, no
slippage reserve, and it does not describe partial fills, unequal fills or
one-directional exposure (which is not frozen and is bounded by the basket stop
instead). `Estimate.excluded` carries that list so a caller cannot print the
number without it.

Currency. Everything scales with `pip_value_per_lot`, which every adapter
documents as *account currency per pip per lot*. No symbol and no currency is
hardcoded here; a different symbol specification simply produces a different
number from the same arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: What the estimate deliberately does not include. Stated once, here, so the
#: engine, the report and the documents all quote the same list.
EXCLUDED_FROM_ESTIMATE = (
    "the exit: closing spread, commission and slippage are not in this figure",
    "swap, which accrues per night held and is not modelled here",
    "partial or unequal fills, where the two sides do not cancel",
    "one-directional exposure, which is not frozen and is bounded by the "
    "basket stop rather than by this number",
    "a cancellation race, a rejected close, or a gap during liquidation",
    "later movement in spread or the broker's minimum stop distance: both are "
    "read once, at admission, and both feed this number",
    "conversion drift when the symbol is not quoted in account currency",
)


@dataclass(frozen=True)
class SymbolSpec:
    """The broker-side inputs. Every one of these is an observation or a fixture.

    `pip_value_per_lot` is account currency per `pip_size` of price movement per
    1.0 lot. On MT5 the adapter derives it from `trade_tick_value` scaled by
    `pip_size / trade_tick_size`; MT5 reports tick value already converted to
    the deposit currency, at the moment it is asked.
    """

    pip_size: float
    pip_value_per_lot: float
    spread: float = 0.0
    #: The EFFECTIVE distance the first level is placed at. It is the larger of a
    #: broker requirement and an application heuristic; the three fields below
    #: attribute it, and only this one changes placement.
    min_stop_distance: float = 0.0
    broker_stop_level_distance: float = 0.0
    app_stop_buffer: float = 0.0
    app_spread_multiple_distance: float = 0.0

    @classmethod
    def from_broker(cls, info) -> "SymbolSpec":
        return cls(pip_size=info.pip_size, pip_value_per_lot=info.pip_value_per_lot,
                   spread=getattr(info, "spread", 0.0) or 0.0,
                   min_stop_distance=getattr(info, "min_stop_distance", 0.0) or 0.0,
                   broker_stop_level_distance=getattr(info, "broker_stop_level_distance", 0.0) or 0.0,
                   app_stop_buffer=getattr(info, "app_stop_buffer", 0.0) or 0.0,
                   app_spread_multiple_distance=getattr(
                       info, "app_spread_multiple_distance", 0.0) or 0.0)

    @property
    def broker_required_distance(self) -> float:
        """What the BROKER requires, with nothing of ours added."""
        return self.broker_stop_level_distance

    @property
    def app_added_distance(self) -> float:
        """How much of the effective distance is this application's choice.

        Zero when the broker's own requirement is the binding term. Positive when
        our buffer or our spread multiple is what pushed the levels out.
        """
        return max(0.0, self.min_stop_distance - self.broker_stop_level_distance)


@dataclass(frozen=True)
class GridSpec:
    """The configured inputs. These are the owner's settings."""

    buy_levels: int
    sell_levels: int
    lot: float
    distance: float


@dataclass(frozen=True)
class Estimate:
    total: float
    displacement_cost: float
    entry_spread_cost: float
    first_step: float
    buy_levels: list
    sell_levels: list
    fills: int
    total_volume: float
    excluded: tuple = field(default=EXCLUDED_FROM_ESTIMATE)

    @property
    def widest_level_distance(self) -> float:
        """How far the outermost order sits from the reference, in price units."""
        distances = [abs(level) for level in
                     ([b - self.reference for b in self.buy_levels] +
                      [self.reference - s for s in self.sell_levels])]
        return max(distances) if distances else 0.0

    reference: float = 0.0


def grid_levels(price: float, grid: GridSpec, symbol: SymbolSpec) -> tuple[list, list]:
    """The exact prices the engine would place, first step included.

    The first step is `max(distance, min_stop_distance)` because a stop order
    closer than the broker's minimum is rejected outright. On MT5 that minimum
    is itself derived from the live spread, so a wider spread pushes every level
    further out and raises the estimate more than linearly.
    """
    first_step = max(grid.distance, symbol.min_stop_distance)
    buys = [price + first_step + i * grid.distance for i in range(grid.buy_levels)]
    sells = [price - first_step - i * grid.distance for i in range(grid.sell_levels)]
    return buys, sells


def completed_grid_estimate(price: float, grid: GridSpec, symbol: SymbolSpec) -> Estimate:
    """The scenario figure, with its components kept separate.

    Returns zero total when `pip_size` is missing rather than guessing one: a
    symbol whose specification could not be read is an unknown, and the caller
    decides what an unknown means. `_entry_gate` refuses on unknowns elsewhere.
    """
    buys, sells = grid_levels(price, grid, symbol)
    if not symbol.pip_size:
        return Estimate(total=0.0, displacement_cost=0.0, entry_spread_cost=0.0,
                        first_step=max(grid.distance, symbol.min_stop_distance),
                        buy_levels=buys, sell_levels=sells, fills=0,
                        total_volume=0.0, reference=price)

    point, per_point = symbol.pip_size, symbol.pip_value_per_lot
    displacement = 0.0
    for level in buys:      # filled above the reference, valued back at it
        displacement += (level - price) / point * grid.lot * per_point
    for level in sells:     # filled below the reference, valued back at it
        displacement += (price - level) / point * grid.lot * per_point

    fills = len(buys) + len(sells)
    entry_spread = (symbol.spread / point) * grid.lot * per_point * fills
    return Estimate(
        total=round(displacement + entry_spread, 2),
        displacement_cost=round(displacement, 2),
        entry_spread_cost=round(entry_spread, 2),
        first_step=max(grid.distance, symbol.min_stop_distance),
        buy_levels=buys, sell_levels=sells, fills=fills,
        total_volume=round(fills * grid.lot, 2), reference=price,
    )
