import pandas as pd
import yfinance as yf

from app.brokers.base import SymbolInfo
from app.risk.risk_manager import RiskManager
from app.strategy.ema_rsi_strategy import EmaRsiStrategy, Signal

_YF_SYMBOL_MAP = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X",
    "USDCHF": "USDCHF=X",
    "USDCAD": "USDCAD=X",
    "XAUUSD": "GC=F",  # Gold futures continuous contract — closest free proxy for spot gold
}

# Local pip-size table so the backtester has no dependency on a live broker connection.
_PIP_SIZES = {
    "EURUSD": 0.0001,
    "GBPUSD": 0.0001,
    "USDJPY": 0.01,
    "AUDUSD": 0.0001,
    "USDCHF": 0.0001,
    "USDCAD": 0.0001,
    "XAUUSD": 0.01,
}


def fetch_history(symbol: str, period: str = "60d", interval: str = "15m") -> pd.DataFrame:
    yf_symbol = _YF_SYMBOL_MAP.get(symbol, f"{symbol}=X")
    df = yf.download(yf_symbol, period=period, interval=interval, progress=False, auto_adjust=True)
    if df.empty:
        raise ValueError(f"No historical data returned for {symbol} ({yf_symbol}). Try a shorter period/interval.")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)
    df.index.name = "time"
    return df[["open", "high", "low", "close", "volume"]]


def run_backtest(
    symbol: str,
    strategy: EmaRsiStrategy,
    risk_manager: RiskManager,
    starting_balance: float = 10_000.0,
    period: str = "60d",
    interval: str = "15m",
    basket_mode: bool = False,
    basket_max_entries: int = 5,
    basket_add_gap_points: float = 50,
    basket_target_usd: float = 3.0,
    basket_max_loss_usd: float = 15.0,
    basket_max_bars: int = 0,
    spread_points: float = 0,
    setup_expiry_minutes: int = 45,
) -> dict:
    df = fetch_history(symbol, period, interval)
    pip_size = _PIP_SIZES.get(symbol, 0.0001)
    symbol_info = SymbolInfo(
        symbol=symbol, pip_size=pip_size, pip_value_per_lot=10.0, min_volume=0.01, volume_step=0.01
    )

    if getattr(strategy, "is_structural", False):
        return _run_smc_backtest(
            df=df,
            symbol=symbol,
            strategy=strategy,
            risk_manager=risk_manager,
            symbol_info=symbol_info,
            pip_size=pip_size,
            starting_balance=starting_balance,
            period=period,
            interval=interval,
            spread_points=spread_points,
            setup_expiry_minutes=setup_expiry_minutes,
        )

    if basket_mode:
        return _run_basket_backtest(
            df=df,
            symbol=symbol,
            strategy=strategy,
            risk_manager=risk_manager,
            symbol_info=symbol_info,
            pip_size=pip_size,
            starting_balance=starting_balance,
            period=period,
            interval=interval,
            max_entries=basket_max_entries,
            add_gap_points=basket_add_gap_points,
            target_usd=basket_target_usd,
            max_loss_usd=basket_max_loss_usd,
            max_bars=basket_max_bars,
            spread_points=spread_points,
        )

    balance = starting_balance
    equity_curve: list[dict] = []
    trades: list[dict] = []
    open_trade: dict | None = None

    # Multi-timeframe strategies build their higher timeframe by resampling, so
    # they need enough bars to fill that lookback too.
    higher_tf_bars = 5 * 25 if getattr(strategy, "higher_timeframe", None) else 0
    min_len = max(strategy.ema_slow_period, strategy.rsi_period, higher_tf_bars) + 2

    for i in range(min_len, len(df)):
        window = df.iloc[: i + 1]
        bar = df.iloc[i]

        if open_trade:
            hit_sl = (open_trade["side"] == "BUY" and bar["low"] <= open_trade["sl"]) or (
                open_trade["side"] == "SELL" and bar["high"] >= open_trade["sl"]
            )
            hit_tp = (open_trade["side"] == "BUY" and bar["high"] >= open_trade["tp"]) or (
                open_trade["side"] == "SELL" and bar["low"] <= open_trade["tp"]
            )
            if hit_sl or hit_tp:
                exit_price = open_trade["sl"] if hit_sl else open_trade["tp"]
                pips = (exit_price - open_trade["entry"]) / pip_size
                if open_trade["side"] == "SELL":
                    pips = -pips
                # Spread is a real cost on every trade and is charged here so
                # single-trade and basket results can be compared like for like.
                profit = round((pips - spread_points) * open_trade["volume"] * 10.0, 2)
                balance = round(balance + profit, 2)
                trades.append(
                    {
                        "side": open_trade["side"],
                        "volume": open_trade["volume"],
                        "entry": round(open_trade["entry"], 5),
                        "exit": round(float(exit_price), 5),
                        "sl": round(open_trade["sl"], 5),
                        "tp": round(open_trade["tp"], 5),
                        "profit": profit,
                        "result": "TP" if hit_tp else "SL",
                        "open_time": str(df.index[open_trade["open_index"]]),
                        "close_time": str(df.index[i]),
                    }
                )
                open_trade = None

        if not open_trade:
            result = strategy.generate_signal(window)
            if result.signal != Signal.NONE:
                decision = risk_manager.evaluate(balance, balance, 0, symbol_info)
                if decision.allowed:
                    side = result.signal.value
                    entry = float(bar["close"])
                    sl, tp = risk_manager.compute_sl_tp(entry, side, pip_size)
                    open_trade = {
                        "side": side,
                        "volume": decision.volume,
                        "entry": entry,
                        "sl": float(sl),
                        "tp": float(tp),
                        "open_index": i,
                    }

        equity_curve.append({"time": str(df.index[i]), "equity": balance})

    return _summarize(symbol, period, interval, starting_balance, balance, trades, equity_curve)


def _summarize(
    symbol: str,
    period: str,
    interval: str,
    starting_balance: float,
    balance: float,
    trades: list[dict],
    equity_curve: list[dict],
) -> dict:
    wins = [t for t in trades if t["profit"] > 0]
    losses = [t for t in trades if t["profit"] <= 0]
    total_profit = round(sum(t["profit"] for t in trades), 2)
    gross_profit = sum(t["profit"] for t in wins)
    gross_loss = abs(sum(t["profit"] for t in losses))

    peak = starting_balance
    max_drawdown = 0.0
    for point in equity_curve:
        peak = max(peak, point["equity"])
        drawdown = (peak - point["equity"]) / peak * 100 if peak else 0
        max_drawdown = max(max_drawdown, drawdown)

    return {
        "symbol": symbol,
        "period": period,
        "interval": interval,
        "starting_balance": starting_balance,
        "ending_balance": balance,
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 2) if trades else 0,
        "total_profit": total_profit,
        # The single biggest loss matters more than usual for basket mode: the
        # pattern is designed to produce many small wins, so a healthy win rate
        # says almost nothing on its own.
        "worst_trade": round(min((t["profit"] for t in trades), default=0.0), 2),
        "best_trade": round(max((t["profit"] for t in trades), default=0.0), 2),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
        "max_drawdown_percent": round(max_drawdown, 2),
        "equity_curve": equity_curve,
        "trades": trades,
    }


def _resample(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return df.resample(f"{minutes}min").agg(agg).dropna()


def _run_smc_backtest(
    df: pd.DataFrame,
    symbol: str,
    strategy,
    risk_manager: RiskManager,
    symbol_info: SymbolInfo,
    pip_size: float,
    starting_balance: float,
    period: str,
    interval: str,
    spread_points: float,
    setup_expiry_minutes: int = 45,
) -> dict:
    """Replays the SMC strategy bar by bar, including the parts of it that live
    in the engine rather than in the strategy: waiting for the entry zone, the
    market fallback when price never comes back, setup expiry, and the R ladder
    that trails the stop.

    The higher timeframes are built by resampling the supplied series, so a
    useful run needs 1-minute data — feeding it 15-minute bars leaves nothing
    to resample M1 structure from.
    """
    m5_full = _resample(df, 5)
    m15_full = _resample(df, 15)
    spread = spread_points * pip_size

    balance = starting_balance
    trades: list[dict] = []
    equity_curve: list[dict] = []
    pending = None
    pending_since = None
    open_trade = None

    expiry = setup_expiry_minutes
    fallback_min_rr = getattr(strategy, "fallback_min_rr", 1.0)
    # Where setups go tells you far more than the trade count alone: a bot that
    # finds structure but never fills is a different problem from one that finds
    # no structure at all.
    armed = expired = broken = 0

    for i in range(200, len(df)):
        bar = df.iloc[i]
        now = df.index[i]

        if open_trade:
            hit = _first_hit(_bar_path(bar), open_trade["side"], open_trade["sl"], open_trade["tp"])
            if hit:
                exit_price = open_trade["sl"] if hit == "GROUP_STOP" else open_trade["tp"]
                direction = 1 if open_trade["side"] == "BUY" else -1
                pips = direction * (exit_price - open_trade["entry"]) / pip_size
                profit = round((pips - spread_points) * open_trade["volume"] * 10.0, 2)
                balance = round(balance + profit, 2)
                trades.append(
                    {
                        "side": open_trade["side"],
                        "volume": open_trade["volume"],
                        "entry": round(open_trade["entry"], 5),
                        "exit": round(float(exit_price), 5),
                        "sl": round(open_trade["initial_sl"], 5),
                        "tp": round(open_trade["tp"], 5),
                        "profit": profit,
                        "result": "SL" if hit == "GROUP_STOP" else "TP",
                        "entered": open_trade["entered"],
                        "open_time": str(df.index[open_trade["open_index"]]),
                        "close_time": str(now),
                    }
                )
                open_trade = None
            else:
                # Trail up the R ladder exactly as the engine does live.
                new_sl = risk_manager.compute_rr_ladder_sl(
                    open_trade["side"], open_trade["entry"], open_trade["risk"],
                    open_trade["sl"], float(bar["close"]), 0.0,
                )
                if new_sl is not None:
                    open_trade["sl"] = new_sl

        if open_trade is None and pending is None:
            k15 = m15_full.index.searchsorted(now, side="right")
            k5 = m5_full.index.searchsorted(now, side="right")
            m15 = m15_full.iloc[max(0, k15 - 200) : k15]
            m5 = m5_full.iloc[max(0, k5 - 200) : k5]
            m1 = df.iloc[max(0, i - 199) : i + 1]
            setup = strategy.generate_setup(m15, m5, m1, float(bar["close"]), pip_size, spread)
            if setup.signal != Signal.NONE:
                pending, pending_since = setup, now
                armed += 1

        elif open_trade is None and pending is not None:
            is_buy = pending.signal == Signal.BUY
            stale = expiry and (now - pending_since).total_seconds() / 60 >= expiry
            zone_broken = bar["low"] < pending.sl if is_buy else bar["high"] > pending.sl
            tapped = bar["low"] <= pending.entry if is_buy else bar["high"] >= pending.entry
            ran_away = bar["high"] >= pending.fallback_price if is_buy else bar["low"] <= pending.fallback_price

            entry_price, entered = None, None
            if tapped:
                entry_price, entered = pending.entry, "tap"
            elif ran_away:
                price = pending.fallback_price
                risk_left = abs(price - pending.sl)
                rr_left = abs(pending.tp - price) / risk_left if risk_left > 0 else 0
                if rr_left >= fallback_min_rr:
                    entry_price, entered = price, "fallback"

            if entry_price is not None:
                risk = abs(entry_price - pending.sl)
                stop_pips = risk / pip_size
                volume = risk_manager.calculate_position_size(balance, symbol_info, stop_pips)
                open_trade = {
                    "side": pending.signal.value, "entry": float(entry_price),
                    "sl": float(pending.sl), "initial_sl": float(pending.sl),
                    "tp": float(pending.tp), "risk": risk, "volume": volume,
                    "open_index": i, "entered": entered,
                }
                pending = pending_since = None
            elif stale or zone_broken:
                expired += 1 if stale else 0
                broken += 0 if stale else 1
                pending = pending_since = None

        floating = 0.0
        if open_trade:
            direction = 1 if open_trade["side"] == "BUY" else -1
            floating = (
                direction * (float(bar["close"]) - open_trade["entry"]) / pip_size - spread_points
            ) * open_trade["volume"] * 10.0
        equity_curve.append({"time": str(now), "equity": round(balance + floating, 2)})

    result = _summarize(symbol, period, interval, starting_balance, balance, trades, equity_curve)
    # How the entries were won matters when judging the fallback rule: tap
    # entries are the planned ones, fallback entries are the trades that would
    # simply have been missed before.
    taps = [t for t in trades if t.get("entered") == "tap"]
    fallbacks = [t for t in trades if t.get("entered") == "fallback"]
    result["entry_breakdown"] = {
        "tap": {"count": len(taps), "profit": round(sum(t["profit"] for t in taps), 2)},
        "fallback": {"count": len(fallbacks), "profit": round(sum(t["profit"] for t in fallbacks), 2)},
    }
    result["setup_funnel"] = {
        "armed": armed,
        "filled": len(trades),
        "expired": expired,
        "invalidated": broken,
    }
    return result


def _bar_path(bar) -> list[float]:
    """The order prices are assumed to have been visited inside one bar.

    A bar only records four numbers, so the route between them is a guess. The
    usual convention is used: an up bar is assumed to have dipped to its low
    before running to its high, a down bar the reverse. This matters a great
    deal for baskets, where the group target and the group stop can both sit
    inside a single bar's range — always resolving that in the strategy's
    favour makes any losing method look good, and always resolving it against
    makes every full basket a loss.
    """
    o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
    return [o, l, h, c] if c >= o else [o, h, l, c]


def _first_hit(path: list[float], side: str, stop_price: float, target_price: float) -> str | None:
    """Walks the assumed intrabar path and reports which level was reached
    first: "GROUP_STOP", "TARGET", or None."""
    for a, b in zip(path, path[1:]):
        lo, hi = min(a, b), max(a, b)
        if side == "BUY":
            hit_stop, hit_target = lo <= stop_price, hi >= target_price
        else:
            hit_stop, hit_target = hi >= stop_price, lo <= target_price
        if hit_stop and hit_target:
            # The segment is monotone, so whichever level lies nearer the
            # segment's start is crossed first.
            return "GROUP_STOP" if abs(stop_price - a) <= abs(target_price - a) else "TARGET"
        if hit_stop:
            return "GROUP_STOP"
        if hit_target:
            return "TARGET"
    return None


def _group_exit_price(legs: list[dict], side: str, target_usd: float, pip_size: float) -> float:
    """The price at which the basket's combined P&L equals target_usd exactly.

    Each leg pays (exit - entry)/pip_size * volume * 10 (negated for SELL), so
    the combined P&L is linear in the exit price and can be solved directly
    rather than approximated by the bar's high or low.
    """
    total_volume = sum(leg["volume"] for leg in legs)
    weighted_entry = sum(leg["entry"] * leg["volume"] for leg in legs)
    direction = 1 if side == "BUY" else -1
    k = target_usd * pip_size / 10.0
    return (weighted_entry + direction * k) / total_volume


def _run_basket_backtest(
    df,
    symbol: str,
    strategy: EmaRsiStrategy,
    risk_manager: RiskManager,
    symbol_info: SymbolInfo,
    pip_size: float,
    starting_balance: float,
    period: str,
    interval: str,
    max_entries: int,
    add_gap_points: float,
    target_usd: float,
    max_loss_usd: float,
    max_bars: int,
    spread_points: float,
) -> dict:
    """Simulates the multi-entry basket: several entries in one direction,
    exited together on combined profit, combined loss, or age.

    Each basket is recorded as one trade, so the statistics describe the
    group's result rather than the many small legs it is made of — counting the
    legs separately is what makes this pattern look like a high win rate.

    Spread is charged on every leg, which is not a detail here: the group
    target is a fixed dollar amount, so the more legs a basket holds the
    smaller the price move it needs — while the cost of opening those legs
    keeps growing. On gold a 24-point spread costs roughly $1.20 per 0.05-lot
    leg, so a five-leg basket has already spent $6 chasing a $3 target.
    """
    balance = starting_balance
    equity_curve: list[dict] = []
    trades: list[dict] = []
    legs: list[dict] = []
    side: str | None = None
    opened_at: int | None = None

    higher_tf_bars = 5 * 25 if getattr(strategy, "higher_timeframe", None) else 0
    min_len = max(strategy.ema_slow_period, strategy.rsi_period, higher_tf_bars) + 2

    def close_basket(exit_price: float, index: int, outcome: str) -> None:
        nonlocal balance, legs, side, opened_at
        direction = 1 if side == "BUY" else -1
        profit = 0.0
        for leg in legs:
            profit += direction * (exit_price - leg["entry"]) / pip_size * leg["volume"] * 10.0
            profit -= spread_points * leg["volume"] * 10.0
        profit = round(profit, 2)
        balance = round(balance + profit, 2)
        trades.append(
            {
                "side": side,
                "volume": round(sum(leg["volume"] for leg in legs), 2),
                "entries": len(legs),
                "entry": round(sum(leg["entry"] * leg["volume"] for leg in legs) / sum(leg["volume"] for leg in legs), 5),
                "exit": round(float(exit_price), 5),
                "profit": profit,
                "result": outcome,
                "open_time": str(df.index[opened_at]),
                "close_time": str(df.index[index]),
            }
        )
        legs = []
        side = None
        opened_at = None

    for i in range(min_len, len(df)):
        window = df.iloc[: i + 1]
        bar = df.iloc[i]

        if legs:
            # The target and stop are the prices at which the group's combined
            # P&L, spread already paid, equals the two limits.
            spread_cost = sum(spread_points * leg["volume"] * 10.0 for leg in legs)
            stop_price = _group_exit_price(legs, side, -max_loss_usd + spread_cost, pip_size)
            target_price = _group_exit_price(legs, side, target_usd + spread_cost, pip_size)
            hit = _first_hit(_bar_path(bar), side, stop_price, target_price)
            if hit == "GROUP_STOP":
                close_basket(stop_price, i, "GROUP_STOP")
            elif hit == "TARGET":
                close_basket(target_price, i, "TARGET")
            elif max_bars and opened_at is not None and (i - opened_at) >= max_bars:
                close_basket(float(bar["close"]), i, "TIME")

        result = strategy.generate_signal(window)
        if result.signal != Signal.NONE:
            wanted_side = result.signal.value
            price = float(bar["close"])
            can_enter = False
            if not legs:
                can_enter = True
            elif wanted_side == side and len(legs) < max_entries:
                direction = 1 if side == "BUY" else -1
                adverse_points = direction * (legs[-1]["entry"] - price) / pip_size
                can_enter = adverse_points >= add_gap_points
            if can_enter:
                decision = risk_manager.evaluate(balance, balance, 0, symbol_info)
                if decision.allowed:
                    if not legs:
                        side = wanted_side
                        opened_at = i
                    legs.append({"entry": price, "volume": decision.volume})

        floating = 0.0
        if legs:
            direction = 1 if side == "BUY" else -1
            close_price = float(bar["close"])
            floating = sum(
                direction * (close_price - leg["entry"]) / pip_size * leg["volume"] * 10.0
                - spread_points * leg["volume"] * 10.0
                for leg in legs
            )
        # Equity, not balance: an open basket's unrealized loss is exactly what
        # this pattern hides, so drawdown has to be measured with it included.
        equity_curve.append({"time": str(df.index[i]), "equity": round(balance + floating, 2)})

    return _summarize(symbol, period, interval, starting_balance, balance, trades, equity_curve)
