from datetime import date

import pandas as pd
from fastapi import APIRouter, HTTPException

from app.api.schemas import BacktestRequest, ModeUpdate, SettingsUpdate, StartRequest, TestOrderRequest
from app.backtest.backtester import run_backtest
from app.bot_manager import bot_manager
from app.brokers.base import OrderSide
from app.db import SessionLocal, TradeRecord
from app.risk.risk_manager import RiskManager
from app.strategy.ema_rsi_strategy import EmaRsiStrategy
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
    return {"ok": True}


@router.post("/stop")
async def stop_bot():
    bot_manager.engine.stop()
    return {"ok": True}


@router.get("/trades")
def get_trades(limit: int = 100):
    with SessionLocal() as session:
        records = session.query(TradeRecord).order_by(TradeRecord.open_time.desc()).limit(limit).all()
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
    with SessionLocal() as session:
        closed = session.query(TradeRecord).filter(TradeRecord.status == "CLOSED").all()
        open_count = session.query(TradeRecord).filter(TradeRecord.status == "OPEN").count()

        closed_total = len(closed)
        total = closed_total + open_count
        # Trades reconciled with an unknown outcome (profit is None) count
        # toward the total but not toward wins/losses/win-rate.
        decided = [r for r in closed if r.profit is not None]
        wins = [r for r in decided if r.profit > 0]
        losses = [r for r in decided if r.profit <= 0]
        total_profit = sum(r.profit or 0 for r in closed)
        gross_profit = sum(r.profit for r in wins)
        gross_loss = abs(sum(r.profit for r in losses))
        avg_win = round(gross_profit / len(wins), 2) if wins else 0
        avg_loss = round(gross_loss / len(losses), 2) if losses else 0
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss else None
        today_records = [r for r in closed if r.close_time and r.close_time.date() == date.today()]
        today_profit = sum(r.profit or 0 for r in today_records)

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
            "today_profit": round(today_profit, 2),
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
    closes = df["close"]
    ema_fast = ema(closes, engine.strategy.ema_fast_period)
    ema_slow = ema(closes, engine.strategy.ema_slow_period)

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
    """Places a market order directly (no strategy, no risk manager) for
    connectivity testing — e.g. confirming the broker/account can actually
    execute trades before trusting the automated bot to do it. Optional
    sl/tp so it's not left with no protection if used on a real account.
    """
    if body.side not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail="side must be 'BUY' or 'SELL'")
    engine = bot_manager.engine
    if engine.mode == "real" and not body.confirm_real:
        raise HTTPException(
            status_code=403, detail="Placing a manual order on a REAL account requires confirm_real=true"
        )
    if not engine.broker.is_connected():
        engine.broker.connect()

    side = OrderSide.BUY if body.side == "BUY" else OrderSide.SELL
    try:
        position = engine.broker.place_order(engine.symbol, side, body.volume, 0.0, 0.0)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    with SessionLocal() as session:
        session.add(
            TradeRecord(
                ticket=position.ticket,
                symbol=position.symbol,
                side=position.side.value,
                volume=position.volume,
                open_price=position.open_price,
                sl=position.sl,
                tp=position.tp,
                mode=engine.mode,
                status="OPEN",
            )
        )
        session.commit()

    return {"ok": True, "ticket": position.ticket, "open_price": position.open_price}


@router.post("/backtest")
def backtest(body: BacktestRequest):
    strategy = EmaRsiStrategy()
    risk_manager = RiskManager(
        risk_percent=body.risk_percent,
        stop_loss_pips=body.stop_loss_pips,
        take_profit_pips=body.take_profit_pips,
        max_open_trades=1,
        max_daily_loss_percent=100,
    )
    try:
        result = run_backtest(
            symbol=body.symbol,
            strategy=strategy,
            risk_manager=risk_manager,
            starting_balance=body.starting_balance,
            period=body.period,
            interval=body.interval,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result
