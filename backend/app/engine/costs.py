"""One cost contract, shared by admission, protection and the fit report.

Why this exists. Three places needed to answer "what will closing this cost",
and each answered it differently. `_estimated_exit_cost` returned an UNVERIFIED
half-spread, the daily loss trigger deliberately ignored it, admission ignored it
too, and the fit report printed its own arithmetic. Three answers to one question
is not a contract, and the differences were invisible.

The four quantities, kept apart because they behave differently
---------------------------------------------------------------

1. **Already inside the broker's figure.** MT5's floating profit is understood to
   be struck at the executable closing side, so the exit spread may already be in
   `position.profit`. Whether it is has never been verified for this adapter, so
   `SymbolInfo.profit_includes_exit_spread` stays `None` and that state is carried
   as an UNKNOWN rather than assumed either way. Subtracting a half-spread on top
   of a figure that already contains it charges the same money twice.

2. **Booked charges.** Swap and commission the broker has ALREADY taken are in
   `position.net_profit`. They are history, not a future cost, and they are never
   added here — that would be the same double count in the other direction.

3. **Future charges.** The commission the closing side will charge. The broker
   does not report this in advance, so it comes from the owner's contract
   specification or it is UNKNOWN.

4. **The owner's assumptions.** Slippage. Nobody can read this off a terminal;
   the owner supplies an observed figure or it is UNKNOWN.

Two readings, because the two directions are not symmetric
----------------------------------------------------------

* `conservative` — for TIGHTENING a profit target. Delaying a close is safe, so
  an unverified upper bound may be used here. This keeps the pre-existing
  behaviour: a basket does not claim its target until it has cleared the worst
  plausible exit.
* `defensible` — for bringing a LOSS exit forward, and for admission. Firing
  early on an assumption nobody verified is not safe, so this is `None` unless
  every component came from a verified source or from the owner.

`None` is never zero. A caller that needs a number and gets `None` must refuse,
not substitute.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

#: Where a component's value came from.
VERIFIED = "verified"          # observed from the broker, or structurally certain
OWNER_STATED = "owner_stated"  # the owner supplied it from their contract
UNKNOWN = "unknown"            # nobody has established it


@dataclass(frozen=True)
class CostComponent:
    name: str
    value: float | None
    provenance: str
    note: str = ""

    @property
    def known(self) -> bool:
        return self.value is not None and self.provenance != UNKNOWN

    def as_dict(self) -> dict:
        return {"name": self.name, "value": self.value, "provenance": self.provenance,
                "note": self.note}


@dataclass(frozen=True)
class ClosingCostInputs:
    """Everything needed to price an exit, with its provenance implied by type.

    `exit_commission_per_lot` and `slippage_points_per_fill` are owner settings
    and default to None, which means UNKNOWN — not zero.
    """

    pip_size: float
    pip_value_per_lot: float
    spread: float
    spread_available: bool = True
    #: None means the broker's semantic has not been verified for this adapter.
    profit_includes_exit_spread: bool | None = None
    exit_commission_per_lot: float | None = None
    slippage_points_per_fill: float | None = None

    @classmethod
    def from_broker(cls, info, *, exit_commission_per_lot=None,
                    slippage_points_per_fill=None) -> "ClosingCostInputs":
        return cls(
            pip_size=getattr(info, "pip_size", 0.0) or 0.0,
            pip_value_per_lot=getattr(info, "pip_value_per_lot", 0.0) or 0.0,
            spread=getattr(info, "spread", 0.0) or 0.0,
            spread_available=bool(getattr(info, "spread_available", True)),
            profit_includes_exit_spread=getattr(info, "profit_includes_exit_spread", None),
            exit_commission_per_lot=exit_commission_per_lot,
            slippage_points_per_fill=slippage_points_per_fill,
        )


@dataclass(frozen=True)
class ClosingCost:
    """The answer, with both readings and the reasoning intact."""

    components: tuple = field(default_factory=tuple)
    #: Upper bound usable for delaying a profit exit. None only when the symbol
    #: cannot be priced at all.
    conservative: float | None = None
    #: Usable for bringing a loss exit forward, or for admission. None when any
    #: component is UNKNOWN.
    defensible: float | None = None
    unknowns: tuple = field(default_factory=tuple)
    notes: tuple = field(default_factory=tuple)

    @property
    def fully_known(self) -> bool:
        return self.defensible is not None

    def as_dict(self) -> dict:
        return {
            "conservative": self.conservative,
            "defensible": self.defensible,
            "fully_known": self.fully_known,
            "unknowns": list(self.unknowns),
            "components": [c.as_dict() for c in self.components],
            "notes": list(self.notes),
        }

    def missing_inputs_message(self) -> str:
        """What the owner has to supply, named so it is actionable."""
        wanted = {
            "exit_commission": "EXIT_COMMISSION_PER_LOT_USD (your contract specification's "
                               "commission per lot, charged on the closing side)",
            "slippage": "SLIPPAGE_POINTS_PER_FILL (points of slippage you have actually "
                        "observed on this symbol)",
            "exit_spread_semantic": "BROKER_PROFIT_INCLUDES_EXIT_SPREAD (whether your "
                                    "broker's floating profit is already struck at the "
                                    "closing side: yes or no, once you have checked)",
            "spread": "a live quote — none is currently available",
            "symbol_valuation": "a usable symbol specification — this one cannot be priced",
        }
        return "; ".join(wanted.get(u, u) for u in self.unknowns)


def closing_cost(volume: float, fills: int, inputs: ClosingCostInputs) -> ClosingCost:
    """What closing `fills` positions of `volume` total lots is still expected to cost.

    Positive numbers are costs. `volume` is the total lots to close; `fills` is
    how many separate positions, because commission and slippage are charged per
    position while the spread is charged per lot.
    """
    components: list[CostComponent] = []
    unknowns: list[str] = []
    notes: list[str] = [
        "swap and commission the broker has ALREADY booked are inside net_profit "
        "and are deliberately not added here — that would charge them twice",
    ]

    if not inputs.pip_size or not math.isfinite(inputs.pip_size) \
            or not inputs.pip_value_per_lot or not math.isfinite(inputs.pip_value_per_lot):
        return ClosingCost(
            components=(), conservative=None, defensible=None,
            unknowns=("symbol_valuation",),
            notes=tuple(notes + ["the symbol specification cannot price anything, so no "
                                 "exit cost can be stated at all"]),
        )

    # 1. The exit spread, and whether it is already counted.
    if not inputs.spread_available:
        spread_cost = None
        components.append(CostComponent("exit_spread", None, UNKNOWN,
                                        "no quote is available, so the closing spread is unknown"))
        unknowns.append("spread")
        conservative_spread = None
    elif inputs.profit_includes_exit_spread is True:
        spread_cost = 0.0
        components.append(CostComponent(
            "exit_spread", 0.0, VERIFIED,
            "the broker's floating profit is already struck at the closing side, so the "
            "exit spread is inside it and is not charged again"))
        conservative_spread = 0.0
    elif inputs.profit_includes_exit_spread is False:
        spread_cost = (inputs.spread / inputs.pip_size) / 2 * volume * inputs.pip_value_per_lot
        components.append(CostComponent("exit_spread", round(spread_cost, 4), VERIFIED,
                                        "half the spread per lot, verified as NOT already "
                                        "inside the broker's figure"))
        conservative_spread = spread_cost
    else:
        # Unverified semantic. Conservative reading assumes it is NOT included
        # (the larger cost); the defensible reading refuses to guess.
        conservative_spread = (inputs.spread / inputs.pip_size) / 2 * volume * inputs.pip_value_per_lot
        spread_cost = None
        components.append(CostComponent(
            "exit_spread", None, UNKNOWN,
            "nobody has verified whether this broker's floating profit already includes "
            "the exit spread, so it is an upper bound for delaying a profit exit and "
            "unusable for bringing a loss exit forward"))
        unknowns.append("exit_spread_semantic")

    # 2. Future commission, from the owner's contract or nowhere.
    if inputs.exit_commission_per_lot is None:
        commission = None
        components.append(CostComponent("exit_commission", None, UNKNOWN,
                                        "the closing side's commission has not been supplied"))
        unknowns.append("exit_commission")
        conservative_commission = 0.0
    else:
        commission = inputs.exit_commission_per_lot * volume
        components.append(CostComponent("exit_commission", round(commission, 4), OWNER_STATED,
                                        "per lot, closing side, from the owner's contract "
                                        "specification"))
        conservative_commission = commission

    # 3. Slippage, an owner assumption or nothing.
    if inputs.slippage_points_per_fill is None:
        slippage = None
        components.append(CostComponent("slippage", None, UNKNOWN,
                                        "no observed slippage has been supplied"))
        unknowns.append("slippage")
        conservative_slippage = 0.0
    else:
        slippage = (inputs.slippage_points_per_fill * volume * inputs.pip_value_per_lot)
        components.append(CostComponent("slippage", round(slippage, 4), OWNER_STATED,
                                        "points per fill x volume, from the owner's own "
                                        "observation"))
        conservative_slippage = slippage

    # The conservative reading never returns None once the symbol is priceable:
    # an unknown component contributes its upper bound, or zero when no bound can
    # be derived. It is only ever used to DELAY a close.
    conservative = round((conservative_spread or 0.0) + conservative_commission
                         + conservative_slippage, 4)
    if conservative_spread is None:
        notes.append("the conservative reading has no spread term because no quote was "
                     "available")

    defensible = None
    if not unknowns:
        defensible = round((spread_cost or 0.0) + (commission or 0.0) + (slippage or 0.0), 4)
    else:
        notes.append("the defensible reading is unavailable: " + ", ".join(unknowns))

    return ClosingCost(components=tuple(components), conservative=conservative,
                       defensible=defensible, unknowns=tuple(unknowns), notes=tuple(notes))
