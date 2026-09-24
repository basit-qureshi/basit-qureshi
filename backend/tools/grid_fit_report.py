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
    symbol.add_argument("--broker-stop-level-points", type=float, default=0.0,
                        help="the BROKER's declared minimum stop distance, in points "
                             "(MT5 symbol_info().trade_stops_level). 0 if it declares none")
    symbol.add_argument("--app-buffer-points", type=float, default=5.0,
                        help="THIS APP's buffer added on top of the broker's minimum, "
                             "in points. Only applied when the broker declares one")
    symbol.add_argument("--app-spread-multiple", type=float, default=3.0,
                        help="THIS APP's fallback: spread x this. No broker states "
                             "this rule; on a wide spread it is usually binding")
    symbol.add_argument("--min-stop-distance", type=float, default=None,
                        help="override the effective distance directly, in price units, "
                             "instead of deriving it from the three inputs above")
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

    closing = parser.add_argument_group(
        "closing costs (YOUR broker's figures; omitted means UNKNOWN, never zero)")
    closing.add_argument("--commission-per-lot-per-side", type=float, default=None,
                         help="from your contract specification or an account statement")
    closing.add_argument("--exit-spread", type=float, default=None,
                         help="spread in price units expected when closing, if it "
                              "differs from the entry spread")
    closing.add_argument("--slippage-points-per-fill", type=float, default=None,
                         help="observed slippage per fill, in points")

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
        ("broker stop level (pts)", "broker_stop_level_points", UNKNOWN),
        ("app buffer (pts)", "app_buffer_points", "APP CHOICE (this project's, not your broker's)"),
        ("app spread multiple", "app_spread_multiple", "APP CHOICE (this project's, not your broker's)"),
        ("buy levels", "buy_levels", CONFIGURED),
        ("sell levels", "sell_levels", CONFIGURED),
        ("lot size", "lot", CONFIGURED),
        ("grid distance", "distance", CONFIGURED),
    ):
        value = getattr(args, name)
        touched = value != getattr(defaults, name)
        rows.append((label, f"{value}", STATED if touched else fixture_origin))
    return rows


def stop_distance(args) -> dict:
    """The three inputs, what each contributes, and which one binds.

    The engine's effective distance is
    `max(broker_minimum + app_buffer, spread * app_multiple)`. Printing only the
    result hides an application choice behind a broker-sounding name.
    """
    broker = args.broker_stop_level_points * args.pip_size
    buffer_ = (args.app_buffer_points * args.pip_size) if args.broker_stop_level_points else 0.0
    spread_side = args.spread * args.app_spread_multiple
    derived = max(broker + buffer_, spread_side)
    effective = args.min_stop_distance if args.min_stop_distance is not None else derived
    if spread_side > broker + buffer_:
        binding = "THIS APP's spread multiple"
    elif broker:
        binding = "the broker's minimum" + (" plus this app's buffer" if buffer_ else "")
    else:
        binding = "nothing — the grid distance itself is wider"
    return {"broker": broker, "buffer": buffer_, "spread_side": spread_side,
            "derived": derived, "effective": effective, "binding": binding,
            "overridden": args.min_stop_distance is not None}


def closing_costs(args, est, cur: str) -> tuple[float | None, list[str]]:
    """What closing this basket would cost, from OWNER-SUPPLIED figures only.

    Returns None when any component was not supplied. An unknown cost is never
    treated as zero and never guessed: the report says UNKNOWN and the budget
    comparison is reported as covering the entry side only.
    """
    lines, total, unknown = [], 0.0, False
    volume = est.total_volume

    if args.commission_per_lot_per_side is not None:
        value = args.commission_per_lot_per_side * volume
        total += value
        lines.append(f"  exit commission       : {value:>10.2f} {cur}   "
                     f"({args.commission_per_lot_per_side} x {volume} lots, closing side)")
    else:
        unknown = True
        lines.append("  exit commission       :    UNKNOWN   "
                     "pass --commission-per-lot-per-side from your contract spec")

    exit_spread = args.exit_spread if args.exit_spread is not None else args.spread
    spread_source = "--exit-spread" if args.exit_spread is not None else "entry spread, assumed"
    if args.pip_size:
        value = (exit_spread / args.pip_size) * args.lot * args.pip_value * est.fills
        total += value
        lines.append(f"  exit spread           : {value:>10.2f} {cur}   "
                     f"({est.fills} fills x {exit_spread}, {spread_source})")

    if args.slippage_points_per_fill is not None:
        value = args.slippage_points_per_fill * args.lot * args.pip_value * est.fills
        total += value
        lines.append(f"  slippage              : {value:>10.2f} {cur}   "
                     f"({args.slippage_points_per_fill} points x {est.fills} fills)")
    else:
        unknown = True
        lines.append("  slippage              :    UNKNOWN   "
                     "pass --slippage-points-per-fill if you have observed it")

    unknown = True      # swap is never modelled here
    lines.append("  swap                  :    UNKNOWN   depends on nights held; "
                 "not modelled by this tool")
    return (None if unknown else round(total, 2)), lines


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    grid = grid_math.GridSpec(buy_levels=args.buy_levels, sell_levels=args.sell_levels,
                              lot=args.lot, distance=args.distance)
    stops = stop_distance(args)
    symbol = grid_math.SymbolSpec(
        pip_size=args.pip_size, pip_value_per_lot=args.pip_value, spread=args.spread,
        min_stop_distance=stops["effective"],
        broker_stop_level_distance=stops["broker"], app_stop_buffer=stops["buffer"],
        app_spread_multiple_distance=stops["spread_side"])
    est = grid_math.completed_grid_estimate(args.price, grid, symbol)
    cur = args.currency

    print("OFFLINE compatibility report — nothing was read from a broker.\n")
    print("Inputs")
    for label, value, origin in provenance(args):
        print(f"  {label:<20} {value:>10}   {origin}")
    print(f"  {'account currency':<20} {cur:>10}   label only\n")

    print("First-step distance — WHERE IT COMES FROM")
    print(f"  broker's own minimum  : {stops['broker']:.5f} price units   "
          f"({args.broker_stop_level_points} points x {args.pip_size})")
    print(f"  + this app's buffer   : {stops['buffer']:.5f} price units   "
          f"({args.app_buffer_points} points, applied only when the broker declares one)")
    print(f"  this app's spread x{args.app_spread_multiple:<4}: {stops['spread_side']:.5f} "
          f"price units   ({args.spread} x {args.app_spread_multiple})")
    print(f"  EFFECTIVE minimum     : {stops['effective']:.5f} price units   "
          f"binding: {stops['binding']}")
    if stops["overridden"]:
        print("  (you overrode the effective distance with --min-stop-distance)")
    print(f"  grid distance         : {args.distance:.5f} price units")
    print("\n  Only the EFFECTIVE minimum changes placement, and the first step is")
    print("  max(grid distance, effective minimum). An application choice that")
    print("  wins that comparison moves every level, so it belongs in the open.")

    print("\nGrid the engine would place")
    print(f"  first step            : {est.first_step:.5f} price units "
          f"(max of grid distance and the effective minimum)")
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

    closing_total, closing_lines = closing_costs(args, est, cur)
    print("\nClosing this basket would also cost — YOUR figures, not invented ones")
    for line in closing_lines:
        print(line)
    if closing_total is None:
        print("  TOTAL                 :    UNKNOWN   at least one component was not")
        print("                                     supplied, so no total is claimed")
    else:
        print(f"  TOTAL                 : {closing_total:>10.2f} {cur}")

    print("\nComparison against the budgets you stated")
    print("  WHAT IS COMPARED: the entry-side estimate against each budget.")
    print("  Admission compares exactly this and no more — closing costs are NOT")
    print("  in the estimate, and the daily budget's remaining figure excludes the")
    print("  exit reserve as well. Neither side of the comparison carries a")
    print("  closing-cost buffer, so a pass here is NOT a claim that the budget")
    print("  covers the whole round trip.")
    judged = 0

    def verdict_for(budget: float, label: str) -> None:
        nonlocal judged
        judged += 1
        if est.total > budget:
            print(f"    -> REFUSED by admission policy ({est.total:.2f} exceeds "
                  f"{budget:.2f})")
            return
        headroom = round(budget - est.total, 2)
        print(f"    -> entry-side estimate fits, {headroom:.2f} {cur} of {label} left over")
        if closing_total is None:
            print(f"       closing costs UNKNOWN: whether {headroom:.2f} covers them is")
            print("       NOT established by this report or by admission")
        elif closing_total > headroom:
            print(f"       but closing costs of {closing_total:.2f} exceed that "
                  f"{headroom:.2f} — the round trip does NOT fit this budget")
        else:
            print(f"       and the {closing_total:.2f} of closing costs you supplied "
                  f"fit inside it")

    if args.basket_stop is not None:
        print(f"  basket stop           : {args.basket_stop:>10.2f} {cur}")
        verdict_for(args.basket_stop, "budget")
    if args.daily_loss is not None:
        remaining = round(args.daily_loss + args.daily_marked, 2)
        print(f"  daily loss budget     : {args.daily_loss:>10.2f} {cur}")
        print(f"    marked so far       : {args.daily_marked:>10.2f} {cur}")
        print(f"    remaining           : {remaining:>10.2f} {cur}")
        verdict_for(remaining, "remaining budget")
    if args.balance is not None and args.floor is not None:
        headroom = round(args.balance - args.floor, 2)
        print(f"  floor headroom        : {headroom:>10.2f} {cur}   "
              f"(balance {args.balance} - floor {args.floor})")
        verdict_for(headroom, "headroom")
    if not judged:
        print("  none stated. Pass --basket-stop / --daily-loss / --balance and")
        print("  --floor to see how this configuration compares with them.")
    else:
        print("\n  'REFUSED by admission policy' is the declared rule, not a")
        print("  prediction. A basket refused on these grounds may well have")
        print("  reached its profit target. The rule refuses it because the budget")
        print("  that would have to absorb the scenario is smaller than the")
        print("  scenario — nothing here forecasts an outcome, and nothing here")
        print("  recommends raising a budget to change a verdict.")

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
