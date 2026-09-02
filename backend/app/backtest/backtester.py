"""Replay of the XAUUSD M1 pending-order grid on historical candles.

The simulation follows the same cycle the live engine does: build a grid, let
stops fill as price reaches them, add up the basket, close everything at the
target, rebuild. What it adds on top of the live engine is the ability to run
years of that in seconds so the strategy can be judged before money is on it.

Two things are modelled carefully because they decide the answer:

- Spread. Every stop order fills half a spread the wrong side of its level, and
  the position is closed at the other side. On gold that is 24-30 points on a
  strategy whose whole target is $10, so a run with spread set to 0 is fiction.
- Intrabar order. A single M1 candle can reach several grid levels and the
  basket target in the same minute. The bar's assumed path (down-then-up on an
  up bar, the reverse on a down bar) is walked in order rather than assuming
  the best or the worst, because on a grid the order of those events is the
  difference between a winning basket and a losing one.

A basket that closes on a bar leaves the rest of that bar empty: the next grid
is not built until the following candle opens, which is what the live engine
does. Rebuilding within the same bar would put fresh stops straight back into
the move that just paid out.
"""

import pandas as pd
import yfinance as yf

_YF_SYMBOL_MAP = {
    "XAUUSD": "GC=F",  # Gold futures continuous contract — closest free proxy for spot gold
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
}

# The size of one point, and what one point on one lot is worth. A standard
# gold lot is 100 ounces and a point is 0.01, so a point is $1.00 per lot — and
# 0.01 lots, which is all this strategy ever trades, earn a cent per point.
_POINT = {"XAUUSD": 0.01, "EURUSD": 0.0001, "GBPUSD": 0.0001, "USDJPY": 0.01}
_VALUE_PER_LOT_PER_POINT = {"XAUUSD": 1.0, "EURUSD": 10.0, "GBPUSD": 10.0, "USDJPY": 10.0}


def fetch_history(symbol: str, period: str = "7d", interval: str = "1m") -> pd.DataFrame:
    yf_symbol = _YF_SYMBOL_MAP.get(symbol, f"{symbol}=X")
    df = yf.download(yf_symbol, period=period, interval=interval, progress=False, auto_adjust=True)
    if df.empty:
        raise ValueError(f"No historical data returned for {symbol} ({yf_symbol}). Try a shorter period/interval.")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)
    df.index.name = "time"
    return df[["open", "high", "low", "close", "volume"]]


def _bar_path(bar) -> list[float]:
    """The order prices are assumed to have been visited inside one bar.

    A bar records four numbers; the route between them is a guess. The usual
    convention is used — an up bar dipped to its low before running to its
    high, a down bar the reverse.
    """
    o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
    return [o, l, h, c] if c >= o else [o, h, l, c]


class _Basket:
    """Positions opened from one grid, and the pending orders still waiting."""

    def __init__(self, reference: float, lot: float, value_per_point: float):
        self.reference = reference
        self.lot = lot
        self.value_per_point = value_per_point
        self.buy_stops: list[float] = []
        self.sell_stops: list[float] = []
        self.positions: list[dict] = []  # {"side", "entry"}
        self.opened_at = None
        self.fills = 0

    def profit_at(self, price: float, point: float, spread: float) -> float:
        """Combined net profit if every position were closed at `price`.

        The closing side of the spread is charged here; the opening side is
        already in each fill price.
        """
        total = 0.0
        for pos in self.positions:
            exit_price = price - spread / 2 if pos["side"] == "BUY" else price + spread / 2
            move = exit_price - pos["entry"] if pos["side"] == "BUY" else pos["entry"] - exit_price
            total += move / point * self.lot * self.value_per_point
        return total

    def price_for_profit(self, target: float, point: float, spread: float) -> float | None:
        """The price at which the basket's combined profit equals `target`.

        Profit is linear in price, so this is solved rather than searched for -
        which is what lets the basket close AT the target instead of wherever
        the bar happened to end.
        """
        if not self.positions:
            return None
        unit = self.lot * self.value_per_point / point  # profit per 1.0 of price move, per position
        net_units = sum(1 if p["side"] == "BUY" else -1 for p in self.positions) * unit
        if abs(net_units) < 1e-12:
            return None  # perfectly hedged: no price reaches the target
        # profit(price) = net_units * price - constant
        constant = 0.0
        for pos in self.positions:
            if pos["side"] == "BUY":
                constant += (pos["entry"] + spread / 2) * unit
            else:
                constant -= (pos["entry"] - spread / 2) * unit
        return (target + constant) / net_units


def run_grid_backtest(
    symbol: str = "XAUUSD",
    period: str = "7d",
    interval: str = "1m",
    starting_balance: float = 10_000.0,
    lot_size: float = 0.01,
    buy_stop_levels: int = 10,
    sell_stop_levels: int = 10,
    grid_distance: float = 0.30,
    basket_take_profit_usd: float = 10.0,
    basket_stop_loss_usd: float = 0.0,
    spread_points: float = 24.0,
    max_daily_loss_usd: float = 100.0,
    max_equity_drawdown_percent: float = 30.0,
    df: pd.DataFrame | None = None,
) -> dict:
    if df is None:
        df = fetch_history(symbol, period, interval)
    point = _POINT.get(symbol, 0.01)
    value_per_point = _VALUE_PER_LOT_PER_POINT.get(symbol, 1.0)
    spread = spread_points * point

    balance = starting_balance
    equity_peak = starting_balance
    baskets: list[dict] = []
    equity_curve: list[dict] = []
    basket: _Basket | None = None
    # Index of the bar a basket was closed on. The next grid waits for a bar
    # after it, matching the live engine: orders are never placed back onto the
    # same M1 candle whose move produced the profit.
    closed_on_bar: int | None = None
    halted_reason: str | None = None
    day = None
    day_realized = 0.0
    max_positions_seen = 0

    def build(price: float, when) -> _Basket:
        b = _Basket(price, lot_size, value_per_point)
        b.buy_stops = [price + (i + 1) * grid_distance for i in range(buy_stop_levels)]
        b.sell_stops = [price - (i + 1) * grid_distance for i in range(sell_stop_levels)]
        b.opened_at = when
        return b

    def close(b: _Basket, price: float, when, outcome: str) -> None:
        nonlocal balance, day_realized
        profit = round(b.profit_at(price, point, spread), 2)
        balance = round(balance + profit, 2)
        day_realized += profit
        baskets.append(
            {
                "reference": round(b.reference, 2),
                "positions": len(b.positions),
                "fills": b.fills,
                "profit": profit,
                "result": outcome,
                "open_time": str(b.opened_at),
                "close_time": str(when),
            }
        )

    for i in range(len(df)):
        bar = df.iloc[i]
        when = df.index[i]

        if day != when.date():
            day = when.date()
            day_realized = 0.0
            halted_reason = None

        if halted_reason:
            equity_curve.append({"time": str(when), "equity": balance})
            continue

        if basket is None:
            if closed_on_bar is not None and i <= closed_on_bar:
                equity_curve.append({"time": str(when), "equity": balance})
                continue
            closed_on_bar = None
            basket = build(float(bar["open"]), when)

        # Walk the bar. Each leg of the path can fill stops and can reach the
        # basket target; both are checked in the order price would have met them.
        for a, b_price in zip(_bar_path(bar), _bar_path(bar)[1:]):
            rising = b_price >= a
            lo, hi = min(a, b_price), max(a, b_price)

            triggered = [lvl for lvl in basket.buy_stops if lo <= lvl <= hi]
            for lvl in sorted(triggered, reverse=not rising):
                basket.buy_stops.remove(lvl)
                basket.positions.append({"side": "BUY", "entry": lvl + spread / 2})
                basket.fills += 1
            triggered = [lvl for lvl in basket.sell_stops if lo <= lvl <= hi]
            for lvl in sorted(triggered, reverse=rising):
                basket.sell_stops.remove(lvl)
                basket.positions.append({"side": "SELL", "entry": lvl - spread / 2})
                basket.fills += 1

            max_positions_seen = max(max_positions_seen, len(basket.positions))
            if not basket.positions:
                continue

            target_price = basket.price_for_profit(basket_take_profit_usd, point, spread)
            if target_price is not None and lo <= target_price <= hi:
                close(basket, target_price, when, "TARGET")
                basket, closed_on_bar = None, i
                break

            if basket_stop_loss_usd > 0:
                stop_price = basket.price_for_profit(-basket_stop_loss_usd, point, spread)
                if stop_price is not None and lo <= stop_price <= hi:
                    close(basket, stop_price, when, "BASKET_STOP")
                    basket, closed_on_bar = None, i
                    break

        floating = basket.profit_at(float(bar["close"]), point, spread) if basket else 0.0
        equity = round(balance + floating, 2)
        equity_peak = max(equity_peak, equity)
        equity_curve.append({"time": str(when), "equity": equity})

        drawdown = (equity_peak - equity) / equity_peak * 100 if equity_peak > 0 else 0
        if max_daily_loss_usd > 0 and day_realized <= -max_daily_loss_usd:
            halted_reason = f"daily loss limit ({day_realized:.2f})"
        elif max_equity_drawdown_percent > 0 and drawdown >= max_equity_drawdown_percent:
            halted_reason = f"equity drawdown {drawdown:.1f}%"
        if halted_reason and basket:
            close(basket, float(bar["close"]), when, "RISK_HALT")
            basket, closed_on_bar = None, i

    wins = [b for b in baskets if b["profit"] > 0]
    losses = [b for b in baskets if b["profit"] <= 0]
    gross_profit = sum(b["profit"] for b in wins)
    gross_loss = abs(sum(b["profit"] for b in losses))

    peak = starting_balance
    max_drawdown = 0.0
    for point_on_curve in equity_curve:
        peak = max(peak, point_on_curve["equity"])
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - point_on_curve["equity"]) / peak * 100)

    return {
        "symbol": symbol,
        "period": period,
        "interval": interval,
        "starting_balance": starting_balance,
        "ending_balance": balance,
        "total_trades": len(baskets),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(baskets) * 100, 2) if baskets else 0,
        "total_profit": round(balance - starting_balance, 2),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
        "max_drawdown_percent": round(max_drawdown, 2),
        # On a grid the worst basket is the number that matters. The design
        # produces a long run of +$10 wins, so a healthy win rate is guaranteed
        # by construction and says nothing on its own.
        "worst_trade": round(min((b["profit"] for b in baskets), default=0.0), 2),
        "best_trade": round(max((b["profit"] for b in baskets), default=0.0), 2),
        "max_positions_open": max_positions_seen,
        "equity_curve": equity_curve,
        "trades": baskets,
    }
