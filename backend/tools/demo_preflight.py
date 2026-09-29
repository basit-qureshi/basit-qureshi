"""Demo pre-flight: read your broker, print what is missing, and stop. NO ORDERS.

    .\\venv\\Scripts\\python.exe tools\\demo_preflight.py

What this does. It asks your terminal the questions the bot will ask before it
would place anything, and prints the answers with YOUR numbers instead of a
fixture's: account type, symbol, what a point is worth, the live spread, the
minimum stop distance and who set it, the completed-grid estimate for your
configuration, and the closing costs it can measure from your own closed deals.
Then it lists exactly which settings are still unset, with the `.env` lines to
add.

What this does NOT do, and cannot:

  * place, modify or close an order — no order call exists in this file
  * start the engine — nothing from `app.engine` is imported
  * change a setting — it prints lines for you to add, and writes nothing
  * run on a REAL account — it refuses and exits before reading anything else

It needs the MT5 terminal open and logged in, because only the terminal knows
these answers. Run it on the demo account you intend to test with.

Two things it will NOT decide for you, because they are your tolerance and not a
fact about your broker: how much you are willing to lose in a day, and the
balance you never want the account traded below. It shows you which values are
arithmetically coherent with your grid; the choice stays yours.
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.engine import costs as cost_contract  # noqa: E402
from app.engine import grid_math  # noqa: E402

OK, WARN, STOP = "  OK  ", " NOTE ", " STOP "


def line(mark: str, text: str) -> None:
    print(f"[{mark}] {text}")


def measured_costs(mt5, symbol: str, days: int) -> dict:
    """Commission and swap THIS ACCOUNT was actually charged, from closed deals.

    Read-only: `history_deals_get` returns what already happened. Nothing here
    guesses a fee — if there is no history, it says so and the setting stays
    unknown.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        deals = mt5.history_deals_get(since, datetime.now(timezone.utc))
    except Exception as exc:
        return {"available": False, "reason": f"history could not be read: {exc}"}
    if not deals:
        return {"available": False,
                "reason": f"no closed deals in the last {days} days on this account"}

    matched = [d for d in deals if getattr(d, "symbol", "") == symbol]
    if not matched:
        return {"available": False,
                "reason": f"no closed {symbol} deals in the last {days} days"}

    volume = sum(abs(getattr(d, "volume", 0.0) or 0.0) for d in matched)
    commission = sum(abs(getattr(d, "commission", 0.0) or 0.0) for d in matched)
    fees = sum(abs(getattr(d, "fee", 0.0) or 0.0) for d in matched)
    swap = sum(getattr(d, "swap", 0.0) or 0.0 for d in matched)
    if volume <= 0:
        return {"available": False, "reason": "the matched deals carried no volume"}
    return {
        "available": True,
        "deals": len(matched),
        "volume": round(volume, 2),
        "commission_total": round(commission + fees, 4),
        "commission_per_lot_both_sides": round((commission + fees) / volume, 4),
        # One side is what the setting wants: the closing side only.
        "commission_per_lot_per_side": round((commission + fees) / volume / 2, 4),
        "swap_total": round(swap, 4),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only demo pre-flight. Places no orders.")
    parser.add_argument("--symbol", default=None,
                        help="exact Market Watch name. Defaults to SYMBOL from your settings")
    parser.add_argument("--history-days", type=int, default=90,
                        help="how far back to look for closed deals when measuring costs")
    parser.add_argument("--allow-real", action="store_true",
                        help="do not pass this. It exists so the refusal below is explicit")
    args = parser.parse_args(argv)

    from app.config import settings

    symbol = args.symbol or settings.symbol
    problems: list[str] = []
    todo: list[str] = []

    print("DEMO PRE-FLIGHT — reads your terminal, places no orders, changes nothing.\n")

    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("MetaTrader5 is not installed. Run this on the Windows machine inside "
              "the backend venv:\n"
              "  .\\venv\\Scripts\\python.exe -m pip install -r requirements-mt5.txt",
              file=sys.stderr)
        return 3

    if not mt5.initialize():
        print(f"mt5.initialize() failed: {mt5.last_error()}\n"
              "Open the MT5 terminal, log in, and leave it running.", file=sys.stderr)
        return 4
    try:
        return _run(mt5, symbol, args, settings, problems, todo)
    finally:
        mt5.shutdown()


def _run(mt5, symbol, args, settings, problems, todo) -> int:
    # --- 1. Account identity, before anything else --------------------------
    account = mt5.account_info()
    if account is None:
        line(STOP, f"account_info() returned nothing: {mt5.last_error()}")
        return 5

    modes = {getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0): "demo",
             getattr(mt5, "ACCOUNT_TRADE_MODE_CONTEST", 1): "contest",
             getattr(mt5, "ACCOUNT_TRADE_MODE_REAL", 2): "real"}
    trade_mode = modes.get(getattr(account, "trade_mode", None), "unknown")
    currency = getattr(account, "currency", "?")
    balance = getattr(account, "balance", None)
    equity = getattr(account, "equity", None)

    print("1. ACCOUNT")
    if trade_mode == "real" and not args.allow_real:
        line(STOP, f"this terminal is logged into a REAL account "
                   f"(login {getattr(account, 'login', '?')}, {getattr(account, 'server', '?')}).")
        print("\n       Nothing further was read. This pre-flight is for the DEMO account")
        print("       you intend to test with. Log the terminal into the demo account")
        print("       and run it again.")
        return 6
    if trade_mode != "demo":
        line(WARN, f"the broker classifies this account as {trade_mode!r}, not 'demo'")
        problems.append(f"account trade_mode is {trade_mode!r}")
    else:
        line(OK, f"demo account, as classified BY THE BROKER (not by the app's setting)")
    line(OK, f"balance {balance} {currency}, equity {equity} {currency}")
    if str(settings.account_type).lower() != trade_mode:
        line(STOP, f"ACCOUNT_TYPE in your .env says {settings.account_type!r} but the broker "
                   f"says {trade_mode!r}. The bot refuses to start on that disagreement.")
        problems.append("ACCOUNT_TYPE disagrees with the broker")
        todo.append(f"ACCOUNT_TYPE={trade_mode}")

    # --- 2. Symbol and valuation -------------------------------------------
    print("\n2. SYMBOL")
    if not mt5.symbol_select(symbol, True):
        line(STOP, f"{symbol!r} could not be selected: {mt5.last_error()}")
        print("       Check the exact name in Market Watch, including the broker's suffix "
              "(Exness often uses XAUUSDm).")
        return 7
    line(OK, f"{symbol} is selectable in Market Watch")

    from app.brokers.mt5_broker import MT5Broker

    broker = MT5Broker.__new__(MT5Broker)
    broker._mt5 = mt5
    broker._selected = {symbol}
    broker._ensure_symbol_selected = lambda s: None
    import threading

    broker._lock = threading.Lock()
    info = broker.get_symbol_info(symbol)

    if not info.valuation_ok:
        line(STOP, "this symbol's specification cannot price a grid:")
        for problem in info.valuation_problems:
            print(f"         - {problem}")
        problems.append("symbol valuation incomplete")
    else:
        line(OK, f"point {info.pip_size}, one point on one lot is worth "
                 f"{info.pip_value_per_lot} {currency}")
    if not info.spread_available:
        line(STOP, "no usable quote — the spread this grid would pay is unknown")
        problems.append("no quote available")
    else:
        line(OK, f"live spread {info.spread:.5f} ({info.spread / info.pip_size:.0f} points)"
                 if info.pip_size else f"live spread {info.spread:.5f}")

    # --- 3. Where the first level goes, and whose rule decides -------------
    print("\n3. FIRST-LEVEL DISTANCE — broker rule vs this app's own")
    breakdown = info.stop_distance_breakdown()
    line(OK, f"broker's declared minimum : {breakdown['broker_stop_level_distance']:.5f}")
    line(OK, f"this app's buffer         : {breakdown['app_stop_buffer']:.5f}")
    line(OK, f"this app's spread x{breakdown['app_spread_multiple']:<4}  : "
             f"{breakdown['app_spread_multiple_distance']:.5f}")
    line(OK, f"EFFECTIVE                 : {breakdown['effective_min_stop_distance']:.5f}  "
             f"(binding: {breakdown['binding']})")
    if breakdown["binding"] == "app_spread_multiple":
        print("       Note: an application choice is what moves your levels here, not a")
        print("       broker requirement. That is disclosed, not hidden.")

    # --- 4. YOUR completed-grid estimate ------------------------------------
    print("\n4. COMPLETED-GRID ESTIMATE for your configuration")
    grid = grid_math.GridSpec(buy_levels=settings.grid_buy_stop_levels,
                              sell_levels=settings.grid_sell_stop_levels,
                              lot=settings.grid_lot_size, distance=settings.grid_distance)
    # Guarded. A pre-flight that raises a traceback is useless exactly when
    # something is wrong, which is the only time anyone runs it.
    price = None
    if info.spread_available and info.valuation_ok:
        try:
            price = broker.get_current_price(symbol)
        except Exception as exc:
            line(STOP, f"the current price could not be read: {exc}")
            problems.append("current price unavailable")
    else:
        line(STOP, "skipped: the symbol specification or the quote is missing, so there "
                   "is nothing to price this against")
    if price is None or not math.isfinite(price) or price <= 0:
        if price is not None:
            line(STOP, f"the current price is unusable ({price!r})")
            problems.append("current price unusable")
        print("\n" + "=" * 74)
        print("NOT READY. Fix these first:")
        for problem in problems:
            print(f"  - {problem}")
        print("=" * 74)
        return 1

    spec = grid_math.SymbolSpec.from_broker(info)
    estimate = grid_math.completed_grid_estimate(price, grid, spec)
    print(f"       {grid.buy_levels} buy + {grid.sell_levels} sell, {grid.lot} lots, "
          f"{grid.distance} spacing, reference {price}")
    if not estimate.valid:
        line(STOP, f"cannot be estimated: {estimate.problem}")
        problems.append("completed-grid estimate unavailable")
        estimate_total = None
    else:
        estimate_total = estimate.total
        line(OK, f"first step {estimate.first_step:.5f}, {estimate.fills} orders, "
                 f"{estimate.total_volume} lots if all fill")
        line(OK, f"ESTIMATE {estimate_total:.2f} {currency}  "
                 f"(displacement {estimate.displacement_cost:.2f} + entry spread "
                 f"{estimate.entry_spread_cost:.2f})")
        print("       This is ONE scenario — every level filled, both sides equal, valued")
        print("       back at the reference. It is NOT a maximum loss: no exit cost, no")
        print("       commission, no swap, no slippage, and it does not describe a")
        print("       one-directional move, which the basket stop bounds instead.")

    # --- 5. Closing costs, measured from your own deals --------------------
    print("\n5. CLOSING COSTS — measured from YOUR closed deals")
    observed = measured_costs(mt5, symbol, args.history_days)
    if observed["available"]:
        line(OK, f"{observed['deals']} closed {symbol} deal(s), {observed['volume']} lots total")
        line(OK, f"commission charged: {observed['commission_total']} {currency} "
                 f"= {observed['commission_per_lot_per_side']} per lot per side")
        line(OK, f"swap over the same deals: {observed['swap_total']} {currency}")
        print(f"       -> EXIT_COMMISSION_PER_LOT_USD={observed['commission_per_lot_per_side']}")
    else:
        line(WARN, observed["reason"])
        print("       Nothing is invented here. Two honest ways forward:")
        print("         a) place ONE manual trade by hand on the demo account, close it,")
        print("            and run this again — the figure then comes from your account;")
        print("         b) read commission per lot off your contract specification.")

    if settings.exit_commission_per_lot_usd is None:
        todo.append("EXIT_COMMISSION_PER_LOT_USD=" +
                    (str(observed["commission_per_lot_per_side"]) if observed["available"]
                     else "<per lot, closing side, from your contract spec>"))
    else:
        line(OK, f"EXIT_COMMISSION_PER_LOT_USD is set to "
                 f"{settings.exit_commission_per_lot_usd}")

    if settings.slippage_points_per_fill is None:
        todo.append("SLIPPAGE_POINTS_PER_FILL=<points you have observed; 0 only if you "
                    "have checked and seen none>")
    else:
        line(OK, f"SLIPPAGE_POINTS_PER_FILL is set to {settings.slippage_points_per_fill}")

    semantic = str(settings.broker_profit_includes_exit_spread).strip().lower()
    if semantic not in ("yes", "no"):
        line(WARN, "BROKER_PROFIT_INCLUDES_EXIT_SPREAD is 'unverified', which blocks new "
                   "entries")
        print("       If you do not know, set it to 'no'. That is the CONSERVATIVE choice:")
        print("       it treats the closing spread as an extra cost, which can only make")
        print("       the bot stricter — a bigger grid estimate, a profit target that is")
        print("       harder to reach, and a loss exit that fires slightly sooner.")
        todo.append("BROKER_PROFIT_INCLUDES_EXIT_SPREAD=no")
    else:
        line(OK, f"BROKER_PROFIT_INCLUDES_EXIT_SPREAD={semantic}")

    # --- 6. Your risk numbers ----------------------------------------------
    print("\n6. YOUR RISK NUMBERS — your tolerance, not a broker fact")
    floor = settings.grid_capital_floor_usd
    basket_stop = settings.grid_basket_stop_loss_usd
    daily = settings.grid_max_daily_loss_usd
    minimum = estimate_total if estimate_total is not None else None

    def judge(name: str, value: float, env: str, description: str) -> None:
        if not value:
            line(WARN, f"{name} is 0 — unset. {description}")
            hint = (f"<at least {minimum:.2f}, your choice>" if minimum
                    else "<your choice>")
            todo.append(f"{env}={hint}")
        elif minimum is not None and value < minimum:
            line(STOP, f"{name} is {value:.2f}, below the {minimum:.2f} estimate — "
                       f"admission will refuse every grid and say so")
            problems.append(f"{name} is below the completed-grid estimate")
        else:
            line(OK, f"{name} is {value:.2f}")

    judge("basket stop", basket_stop, "GRID_BASKET_STOP_LOSS_USD",
          "Nothing would end a losing basket.")
    judge("daily loss limit", daily, "GRID_MAX_DAILY_LOSS_USD",
          "Judged on the day's MARKED result, floating loss included.")
    if not floor:
        line(WARN, "capital floor is 0 — unset, and that BLOCKS every new grid by design")
        if balance and minimum:
            print(f"       Coherent with this {balance} {currency} balance: anything below "
                  f"{balance - minimum:.2f}")
            print(f"       leaves room for one completed grid. Lower means more room. The")
            print(f"       number is yours to choose.")
        todo.append("GRID_CAPITAL_FLOOR_USD=<the balance this account must never trade "
                    "below - your choice>")
    elif balance and minimum and (balance - floor) < minimum:
        line(STOP, f"floor {floor:.2f} leaves {balance - floor:.2f} of headroom, less than "
                   f"the {minimum:.2f} estimate — admission will refuse")
        problems.append("floor headroom is below the completed-grid estimate")
    else:
        line(OK, f"capital floor is {floor:.2f}")

    # --- 7. What is open right now -----------------------------------------
    print("\n7. WHAT IS OPEN RIGHT NOW")
    magic = settings.grid_magic_number
    try:
        positions = mt5.positions_get(symbol=symbol) or []
        orders = mt5.orders_get(symbol=symbol) or []
    except Exception as exc:
        line(WARN, f"could not read positions/orders: {exc}")
        positions, orders = [], []
    owned_positions = [p for p in positions if getattr(p, "magic", None) == magic]
    owned_orders = [o for o in orders if getattr(o, "magic", None) == magic]
    line(OK, f"{len(owned_positions)} position(s) and {len(owned_orders)} order(s) carry this "
             f"bot's magic number {magic}")
    others = len(positions) - len(owned_positions) + len(orders) - len(owned_orders)
    if others:
        line(OK, f"{others} other position(s)/order(s) on {symbol} are NOT this bot's and "
                 f"will never be touched by it")

    # --- verdict ------------------------------------------------------------
    print("\n" + "=" * 74)
    if problems:
        print("NOT READY. Fix these first:")
        for problem in problems:
            print(f"  - {problem}")
    elif todo:
        print("ALMOST READY. Nothing is broken; these settings are not chosen yet.")
    else:
        print("Every check this tool can make has passed.")

    if todo:
        print("\nAdd these lines to backend\\.env, then restart the backend:\n")
        for entry in dict.fromkeys(todo):
            print(f"    {entry}")
        print("\n  A value in <angle brackets> is yours to decide. Nothing here fills one in")
        print("  for you, and no line raises a limit to make a check pass.")

    print("\nWhat this pre-flight does NOT tell you: whether this strategy makes money.")
    print("It has never been measured on real price history. A demo run measures the")
    print("machinery, not the edge.")
    print("=" * 74)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
