from datetime import date

from fastapi import APIRouter, HTTPException

from app.api.schemas import BacktestRequest, ModeUpdate, SettingsUpdate, StartRequest
from app.backtest.backtester import run_backtest
from app.bot_manager import bot_manager
from app.db import SessionLocal, TradeRecord
from app.risk.risk_manager import RiskManager
from app.strategy.ema_rsi_strategy import EmaRsiStrategy

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
