import pandas as pd
from fastapi import APIRouter, HTTPException

from app.api.schemas import BacktestRequest, ModeUpdate, SettingsUpdate, StartRequest, TestOrderRequest
from app.backtest.backtester import run_grid_backtest
from app.bot_manager import bot_manager
from app.brokers.base import OrderSide
from app import db as db_module
from app.db import TradeRecord
from app.strategy.indicators import ema

router = APIRouter(prefix="/api")


@router.get("/status")
def get_status():
    engine = bot_manager.engine
    account = None
    if engine.broker.is_connected():
        info = engine.broker.get_account_info()
        account = {
            "balance": info.balance,
            "equity": info.equity,
            "currency": info.currency,
            "leverage": info.leverage,
        }
    return {**engine.status(), "settings": bot_manager.settings, "account": account}


@router.post("/start")
async def start_bot(body: StartRequest):
    # Must run on the main event loop (not FastAPI's sync threadpool) since
    # engine.start() schedules an asyncio task on the currently running loop.
    try:
        bot_manager.engine.start(confirm_real=body.confirm_real)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True}


@router.post("/stop")
async def stop_bot():
    bot_manager.engine.stop()
    return {"ok": True}


@router.get("/trades")
def get_trades(limit: int = 100):
    bot_manager.engine.daily_summary()
    with db_module.SessionLocal() as session:
        records = session.query(TradeRecord).filter_by(
            account_id=bot_manager.engine._account_id, symbol=bot_manager.engine.symbol,
            magic=bot_manager.engine.magic_number, mode=bot_manager.engine.mode,
        ).filter(TradeRecord.status != "DUPLICATE").order_by(TradeRecord.open_time.desc()).limit(max(1, min(limit, 1000))).all()
        return [
            {
                "id": r.id,
                "ticket": r.ticket,
                "symbol": r.symbol,
                "side": r.side,
                "volume": r.volume,
                "open_price": r.open_price,
                "close_price": r.close_price,
                "sl": r.sl,
                "tp": r.tp,
                "profit": r.profit,
                "mode": r.mode,
                "status": r.status,
                "open_time": r.open_time.isoformat() if r.open_time else None,
                "close_time": r.close_time.isoformat() if r.close_time else None,
            }
            for r in records
        ]


@router.get("/stats")
def get_stats():
    bot_manager.engine.daily_summary()
    identity = bot_manager.engine._account_id
    with db_module.SessionLocal() as session:
        query = session.query(TradeRecord).filter_by(
            account_id=identity, symbol=bot_manager.engine.symbol,
            magic=bot_manager.engine.magic_number, mode=bot_manager.engine.mode,
        )
        closed = query.filter(TradeRecord.status == "CLOSED").all()
        open_count = query.filter(TradeRecord.status == "OPEN").count()

        closed_total = len(closed)
        total = closed_total + open_count
        # Trades reconciled with an unknown outcome (profit is None) count
        # toward the total but not toward wins/losses/win-rate.
        decided = [r for r in closed if r.profit is not None]
        wins = [r for r in decided if r.profit > 0]
        losses = [r for r in decided if r.profit < 0]
        total_profit = sum(r.profit or 0 for r in closed)
        gross_profit = sum(r.profit for r in wins)
        gross_loss = abs(sum(r.profit for r in losses))
        avg_win = round(gross_profit / len(wins), 2) if wins else 0
        avg_loss = round(gross_loss / len(losses), 2) if losses else 0
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss else None
        # The daily figures come from the engine's own accounting source, scoped
        # to this bot's symbol, mode, magic number and broker trading day, so a
        # manual test order or another EA cannot move them and the dashboard can
        # never disagree with the number the daily halt was judged on.
        daily = bot_manager.engine.daily_summary()

        ordered = sorted(closed, key=lambda r: r.close_time or r.open_time)
        equity_curve = []
        running = 0.0
        for r in ordered:
            running += r.profit or 0
            timestamp = r.close_time or r.open_time
            equity_curve.append({"time": timestamp.isoformat(), "equity": round(running, 2)})

        return {
            "total_trades": total,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(decided) * 100, 2) if decided else 0,
            "total_profit": round(total_profit, 2),
            # Kept so anything still reading the old field keeps working; it is
            # the same number as today_net_profit_usd.
            "today_profit": daily["today_net_profit_usd"],
            "today_gross_profit_usd": daily["today_gross_profit_usd"],
            "today_gross_loss_usd": daily["today_gross_loss_usd"],
            "today_net_profit_usd": daily["today_net_profit_usd"],
            "today_unsettled_trades": daily["today_unsettled_trades"],
            "daily_target": bot_manager.engine.daily_profit_target_usd,
            "daily_target_hit": bot_manager.engine._daily_target_hit,
            "trading_day": bot_manager.engine._trading_day,
            "open_trades": open_count,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_factor": profit_factor,
            "equity_curve": equity_curve,
        }


@router.get("/candles")
def get_candles(count: int = 200):
    engine = bot_manager.engine
    if not engine.broker.is_connected():
        engine.broker.connect()
    df = engine.broker.get_candles(engine.symbol, engine.timeframe, count)
    # The grid uses no indicators. These two lines are drawn purely so the
    # chart is readable, and nothing in the strategy reads them.
    closes = df["close"]
    ema_fast = ema(closes, 9)
    ema_slow = ema(closes, 21)

    # Truncating to whole seconds can make two distinct timestamps collide
    # (e.g. a synthetic candle generated a fraction of a second after the
    # previous one). The chart library requires strictly increasing times,
    # so bump any collision forward by a second rather than dropping data.
    times: list[int] = []
    for t in df.index:
        ts = int(pd.Timestamp(t).timestamp())
        if times and ts <= times[-1]:
            ts = times[-1] + 1
        times.append(ts)

    candles = [
        {"time": t, "open": float(o), "high": float(h), "low": float(l), "close": float(c)}
        for t, o, h, l, c in zip(times, df["open"], df["high"], df["low"], df["close"])
    ]
    return {
        "symbol": engine.symbol,
        "timeframe": engine.timeframe,
        "candles": candles,
        "ema_fast": [{"time": t, "value": float(v)} for t, v in zip(times, ema_fast)],
        "ema_slow": [{"time": t, "value": float(v)} for t, v in zip(times, ema_slow)],
    }


@router.get("/settings")
def get_settings():
    return bot_manager.settings


@router.post("/settings")
def update_settings(body: SettingsUpdate):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        bot_manager.update_settings(updates)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return bot_manager.settings


@router.post("/mode")
def set_mode(body: ModeUpdate):
    if body.mode not in ("demo", "real"):
        raise HTTPException(status_code=400, detail="mode must be 'demo' or 'real'")
    if body.mode == "real" and not body.confirm:
        raise HTTPException(status_code=400, detail="Switching to REAL account requires confirm=true")
    try:
        bot_manager.set_mode(body.mode)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "mode": body.mode}


@router.post("/test-order")
def test_order(body: TestOrderRequest):
    raise HTTPException(status_code=409, detail="Unprotected manual test orders are disabled; use MT5 demo directly")


@router.post("/backtest")
def backtest(body: BacktestRequest):
    try:
        return run_grid_backtest(
            symbol=body.symbol,
            period=body.period,
            interval=body.interval,
            starting_balance=body.starting_balance,
            lot_size=body.lot_size,
            buy_stop_levels=body.buy_stop_levels,
            sell_stop_levels=body.sell_stop_levels,
            grid_distance=body.grid_distance,
            basket_take_profit_usd=body.basket_take_profit_usd,
            daily_profit_target_usd=body.daily_profit_target_usd,
            basket_stop_loss_usd=body.basket_stop_loss_usd,
            spread_points=body.spread_points,
            max_daily_loss_usd=body.max_daily_loss_usd,
            max_equity_drawdown_percent=body.max_equity_drawdown_percent,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
