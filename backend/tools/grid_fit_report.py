"""Offline compatibility report: does a grid configuration fit stated budgets?

    .\\venv\\Scripts\\python.exe tools\\grid_fit_report.py
    .\\venv\\Scripts\\python.exe tools\\grid_fit_report.py --spread 0.25 --min-stop-distance 0.75
    .\\venv\\Scripts\\python.exe tools\\grid_fit_report.py --basket-stop 60 --daily-loss 100 --balance 1000 --floor 800

This reads NOTHING. It opens no terminal, no network connection and no database,
and it cannot place an order. Every input is a command-line argument, printed
back with its provenance, because the answer is only worth as much as the inputs.

It exists because "about $37.80" is not a fact about gold. It is what this
project's TEST FIXTURE produces. Your broker's minimum stop distance, spread and
tick value give a different number, and on MT5 the minimum stop distance is
itself derived from the live spread — so a wider spread pushes every level
further out and raises the figure more than linearly.

The estimate is one scenario, not a maximum loss. `--assumptions` prints what it
leaves out. Nothing here recommends a configuration, and nothing here changes
one: the active strategy is whatever your settings say.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.engine import grid_math  # noqa: E402

#: Where a number came from, because that decides how much it is worth.
FIXTURE = "FIXTURE  (this project's test double, not your broker)"
CONFIGURED = "CONFIGURED (a setting you chose)"
STATED = "STATED   (you passed it on the command line)"
UNKNOWN = "UNKNOWN  (nobody has observed this from your broker)"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline: does a grid fit stated budgets? Reads nothing.")
    symbol = parser.add_argument_group("symbol specification (from YOUR broker)")
    symbol.add_argument("--price", type=float, default=4000.0,
                        help="reference price the grid would be built around")
    symbol.add_argument("--pip-size", type=float, default=0.01,
                        help="price movement of one point/pip (gold, 2 digits: 0.01)")
    symbol.add_argument("--pip-value", type=float, default=1.0,
                        help="ACCOUNT CURRENCY per pip per 1.0 lot (gold on a USD "
                             "account, 100oz contract: 1.0)")
    symbol.add_argument("--spread", type=float, default=0.24,
                        help="ask - bid in price units at the moment of entry")
    symbol.add_argument("--min-stop-distance", type=float, default=0.0,
                        help="broker minimum distance for a stop order, in price "
                             "units. On MT5 the adapter uses "
                             "max((stops_level+5)*point, spread*3)")
    symbol.add_argument("--currency", default="account currency",
                        help="label only, so the output does not imply USD")

    grid = parser.add_argument_group("grid configuration (YOUR settings)")
    grid.add_argument("--buy-levels", type=int, default=10)
    grid.add_argument("--sell-levels", type=int, default=10)
    grid.add_argument("--lot", type=float, default=0.01)
    grid.add_argument("--distance", type=float, default=0.30)

    budgets = parser.add_argument_group("budgets (optional; omitted ones are not judged)")
    budgets.add_argument("--basket-stop", type=float, default=None)
    budgets.add_argument("--daily-loss", type=float, default=None)
    budgets.add_argument("--daily-marked", type=float, default=0.0,
                         help="today's marked result so far, negative on a losing "
                              "day. The comparison uses what is LEFT")
    budgets.add_argument("--balance", type=float, default=None)
    budgets.add_argument("--floor", type=float, default=None)

    parser.add_argument("--alternatives", action="store_true",
                        help="also print UNTESTED smaller profiles for a later decision")
    parser.add_argument("--assumptions", action="store_true",
                        help="print the full assumption and exclusion list")
    return parser


def provenance(args) -> list[tuple[str, str, str]]:
    """Every input, its value, and where it came from."""
    defaults = build_parser().parse_args([])
    rows = []
    for label, name, fixture_origin in (
        ("reference price", "price", FIXTURE),
        ("pip size", "pip_size", FIXTURE),
        ("pip value per lot", "pip_value", FIXTURE),
        ("spread", "spread", FIXTURE),
        ("min stop distance", "min_stop_distance", FIXTURE),
        ("buy levels", "buy_levels", CONFIGURED),
        ("sell levels", "sell_levels", CONFIGURED),
        ("lot size", "lot", CONFIGURED),
        ("grid distance", "distance", CONFIGURED),
    ):
        value = getattr(args, name)
        touched = value != getattr(defaults, name)
        rows.append((label, f"{value}", STATED if touched else fixture_origin))
    return rows


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    grid = grid_math.GridSpec(buy_levels=args.buy_levels, sell_levels=args.sell_levels,
                              lot=args.lot, distance=args.distance)
    symbol = grid_math.SymbolSpec(pip_size=args.pip_size, pip_value_per_lot=args.pip_value,
                                  spread=args.spread, min_stop_distance=args.min_stop_distance)
    est = grid_math.completed_grid_estimate(args.price, grid, symbol)
    cur = args.currency

    print("OFFLINE compatibility report — nothing was read from a broker.\n")
    print("Inputs")
    for label, value, origin in provenance(args):
        print(f"  {label:<20} {value:>10}   {origin}")
    print(f"  {'account currency':<20} {cur:>10}   label only\n")

    print("Grid the engine would place")
    print(f"  first step            : {est.first_step:.5f} price units "
          f"(max of distance and min stop distance)")
    print(f"  orders                : {grid.buy_levels} BUY STOP + {grid.sell_levels} SELL STOP "
          f"= {est.fills}")
    print(f"  total volume if all fill: {est.total_volume} lots")
    if est.buy_levels:
        print(f"  outermost BUY         : {est.buy_levels[-1]:.5f} "
              f"({est.buy_levels[-1] - args.price:+.5f} from reference)")
    if est.sell_levels:
        print(f"  outermost SELL        : {est.sell_levels[-1]:.5f} "
              f"({est.sell_levels[-1] - args.price:+.5f} from reference)")

    print("\nCompleted-grid estimate — ONE scenario: every level filled, equal")
    print("volume both sides, valued as if closed back at the reference price.")
    print(f"  displacement component: {est.displacement_cost:>10.2f} {cur}")
    print(f"  entry spread component: {est.entry_spread_cost:>10.2f} {cur}   "
          f"({est.fills} fills x {args.spread} spread)")
    print(f"  ESTIMATE              : {est.total:>10.2f} {cur}")
    print("\n  This is NOT a maximum loss. It has no exit cost, no commission,")
    print("  no swap and no slippage reserve. Run with --assumptions for the list.")

    print("\nFit against the budgets you stated")
    judged = 0
    if args.basket_stop is not None:
        judged += 1
        verdict = "FITS" if est.total <= args.basket_stop else "REFUSED by policy"
        print(f"  basket stop           : {args.basket_stop:>10.2f} {cur}   -> {verdict}")
    if args.daily_loss is not None:
        judged += 1
        remaining = round(args.daily_loss + args.daily_marked, 2)
        verdict = "FITS" if est.total <= remaining else "REFUSED by policy"
        print(f"  daily loss budget     : {args.daily_loss:>10.2f} {cur}")
        print(f"    marked so far       : {args.daily_marked:>10.2f} {cur}")
        print(f"    remaining           : {remaining:>10.2f} {cur}   -> {verdict}")
    if args.balance is not None and args.floor is not None:
        judged += 1
        headroom = round(args.balance - args.floor, 2)
        verdict = "FITS" if est.total <= headroom else "REFUSED by policy"
        print(f"  floor headroom        : {headroom:>10.2f} {cur}   "
              f"(balance {args.balance} - floor {args.floor}) -> {verdict}")
    if not judged:
        print("  none stated. Pass --basket-stop / --daily-loss / --balance and")
        print("  --floor to see how this configuration compares with them.")
    else:
        print("\n  'REFUSED by policy' is the declared admission rule, not a")
        print("  prediction. A basket admitted with less headroom than the")
        print("  estimate may still reach its profit target and never approach")
        print("  this scenario. The rule refuses it because the budget that")
        print("  would have to absorb the scenario is smaller than the scenario.")

    if args.alternatives:
        print("\nUNTESTED alternative profiles — arithmetic only, for a LATER")
        print("explicit decision. None of these is tested, recommended, or known")
        print("to be profitable, and none of them is active.")
        print(f"  {'levels':>8} {'lot':>6} {'distance':>9} {'estimate':>10}")
        for levels in (10, 8, 6, 5, 3):
            for lot in (args.lot,):
                for distance in (args.distance, round(args.distance / 2, 5)):
                    alt = grid_math.GridSpec(buy_levels=levels, sell_levels=levels,
                                             lot=lot, distance=distance)
                    value = grid_math.completed_grid_estimate(args.price, alt, symbol).total
                    print(f"  {f'{levels}+{levels}':>8} {lot:>6} {distance:>9} {value:>10.2f}")
        print("  A smaller grid has a smaller frozen scenario AND a smaller")
        print("  cash target. Nothing here says the trade-off is favourable;")
        print("  that question needs tick history, not arithmetic.")

    if args.assumptions:
        print("\nAssumptions inside the estimate")
        for line in (
            "every configured level fills, at exactly its own price",
            "the two sides end with equal volume, so they cancel",
            "the entry spread is paid once per fill, at the stated width",
            "the basket is valued as if closed back at the reference price",
            "pip value per lot is constant and already in account currency",
            "the minimum stop distance at admission is the one that applies",
        ):
            print(f"  + {line}")
        print("\nExcluded from the estimate")
        for line in grid_math.EXCLUDED_FROM_ESTIMATE:
            print(f"  - {line}")
        print("\nMT5's order_calc_profit would estimate a specified operation in")
        print("account currency. It is not a worst-case path loss and not a")
        print("settlement figure, and this tool does not call it: no terminal is")
        print("touched here.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
