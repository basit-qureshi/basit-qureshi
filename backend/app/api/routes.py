from datetime import datetime, timedelta, timezone
import pandas as pd
from fastapi import APIRouter, HTTPException
from sqlalchemy import and_, or_

from app.api.schemas import BacktestRequest, ModeUpdate, SettingsUpdate, StartRequest, TestOrderRequest
from app.backtest.backtester import run_grid_backtest
from app.bot_manager import bot_manager
from app.brokers.base import OrderSide, PendingType
from app import db as db_module
from app.db import TradeRecord
from app.strategy.indicators import ema

router = APIRouter(prefix="/api")


def utc_iso(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _day_start(day: str) -> datetime:
    """Midnight of a YYYY-MM-DD day, for rows too old to carry a trading_day.

    Stored times are naive UTC, so the bound is naive UTC too. This is only a
    fallback: anything the engine settled has an explicit trading_day and is
    compared against that instead.
    """
    try:
        return datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail=f"date must be YYYY-MM-DD, got {day!r}")


def _day_after(day: str) -> datetime:
    return _day_start(day) + timedelta(days=1)



@router.get("/status")
async def get_status():
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


@router.post("/clear-halt")
async def clear_halt():
    """Owner action to release a risk halt. It refuses while this bot still owns
    any position or resting order, because clearing a halt over live exposure is
    how one breach becomes a larger one."""
    ok, message = bot_manager.engine.clear_halt()
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    return {"ok": True, "message": message}


@router.post("/stop")
async def stop_bot():
    bot_manager.engine.stop()
    return {"ok": True}


def _trade_json(r: TradeRecord) -> dict:
    return {
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
        "trading_day": r.trading_day,
        # Why it ended, in the engine's words. None for rows written before the
        # column existed, and for trades that ended outside a basket close.
        "close_reason": r.close_reason,
        "open_time": utc_iso(r.open_time),
        "close_time": utc_iso(r.close_time),
    }


@router.get("/trades")
async def get_trades(
    page: int = 1,
    page_size: int = 50,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    side: str | None = None,
    result: str | None = None,
    search: str | None = None,
    limit: int | None = None,
):
    """This bot's own trades, filtered and paged.

    Every filter narrows the same query the totals are taken from, so a page of
    rows and the summary above it can never disagree. The scope is always this
    account, symbol, magic number and mode - a manual order or another EA is
    not history this bot may claim.

    Dates are broker trading days (YYYY-MM-DD), not machine dates, so a filter
    divides days exactly where the daily accounting does. Rows old enough to
    have no trading_day stamp fall back to their open time.
    """
    engine = bot_manager.engine
    engine.daily_summary()

    # `limit` was the old parameter. Honour it as a page size so anything still
    # calling the previous shape keeps working.
    if limit is not None:
        page_size = limit
    page = max(1, page)
    page_size = max(1, min(page_size, 500))

    with db_module.SessionLocal() as session:
        query = session.query(TradeRecord).filter_by(
            account_id=engine._account_id, symbol=engine.symbol,
            magic=engine.magic_number, mode=engine.mode,
        ).filter(TradeRecord.status != "DUPLICATE")

        if date_from:
            query = query.filter(
                or_(
                    TradeRecord.trading_day >= date_from,
                    and_(TradeRecord.trading_day.is_(None), TradeRecord.open_time >= _day_start(date_from)),
                )
            )
        if date_to:
            query = query.filter(
                or_(
                    TradeRecord.trading_day <= date_to,
                    and_(TradeRecord.trading_day.is_(None), TradeRecord.open_time < _day_after(date_to)),
                )
            )
        if status and status.upper() != "ALL":
            query = query.filter(TradeRecord.status == status.upper())
        if side and side.upper() != "ALL":
            query = query.filter(TradeRecord.side == side.upper())
        if result and result.lower() != "all":
            key = result.lower()
            if key == "win":
                query = query.filter(TradeRecord.profit > 0)
            elif key == "loss":
                query = query.filter(TradeRecord.profit < 0)
            elif key == "breakeven":
                query = query.filter(TradeRecord.profit == 0)
            elif key == "unsettled":
                # Closed at the broker but with no figure yet, or still open.
                query = query.filter(TradeRecord.profit.is_(None))
        if search:
            like = f"%{search.strip()}%"
            query = query.filter(or_(TradeRecord.ticket.like(like), TradeRecord.close_reason.like(like)))

        total = query.count()
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
        rows = (
            query.order_by(TradeRecord.open_time.desc(), TradeRecord.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        # Totals for the WHOLE filtered selection, not just the page on screen -
        # otherwise paging would appear to change the result.
        decided = [r.profit for r in query.all() if r.profit is not None]
        wins = [p for p in decided if p > 0]
        losses = [p for p in decided if p < 0]

        return {
            "items": [_trade_json(r) for r in rows],
            "page": page,
            "page_size": page_size,
            "pages": pages,
            "total": total,
            "showing": len(rows),
            "selection": {
                "net_profit": round(sum(decided), 2),
                "gross_profit": round(sum(wins), 2),
                "gross_loss": round(sum(losses), 2),
                "wins": len(wins),
                "losses": len(losses),
                "settled": len(decided),
                "unsettled": total - len(decided),
                "win_rate": round(len(wins) / len(decided) * 100, 2) if decided else 0.0,
            },
        }


@router.get("/trading-days")
async def get_trading_days():
    """The broker trading days this bot actually has trades on, newest first.

    The date pickers offer these rather than a blank calendar, so a range can
    only be built from days that exist.
    """
    engine = bot_manager.engine
    with db_module.SessionLocal() as session:
        rows = (
            session.query(TradeRecord.trading_day)
            .filter_by(account_id=engine._account_id, symbol=engine.symbol,
                       magic=engine.magic_number, mode=engine.mode)
            .filter(TradeRecord.status != "DUPLICATE", TradeRecord.trading_day.isnot(None))
            .distinct().all()
        )
    days = sorted({r[0] for r in rows}, reverse=True)
    return {"days": days, "first": days[-1] if days else None, "last": days[0] if days else None}


@router.get("/open-trades")
async def get_open_trades():
    """What this bot is holding right now, priced live.

    Read from the broker rather than from the trade table, because the table
    only knows what was last settled. Net is after swap and commission; the
    basket rule is judged on the same figure, so this panel and the basket
    total cannot disagree.
    """
    engine = bot_manager.engine
    if not engine.broker.is_connected():
        # Same as the chart endpoint: connect on demand so the panel is useful
        # before Start is pressed, rather than sitting blank.
        try:
            engine.broker.connect()
        except Exception as exc:
            return {"connected": False, "positions": [], "totals": None, "error": str(exc)}

    try:
        positions = engine.broker.get_open_positions(engine.symbol, magic=engine.magic_number)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"could not read open positions: {exc}")

    try:
        pendings = engine.broker.get_pending_orders(engine.symbol, magic=engine.magic_number)
    except Exception:
        pendings = []

    opened_at = {}
    with db_module.SessionLocal() as session:
        for r in session.query(TradeRecord).filter_by(
            account_id=engine._account_id, symbol=engine.symbol, magic=engine.magic_number,
        ).filter(TradeRecord.status == "OPEN").all():
            opened_at[r.ticket] = utc_iso(r.open_time)

    try:
        price = engine.broker.get_current_price(engine.symbol)
    except Exception:
        price = None

    items = []
    for p in positions:
        key = p.identifier or p.ticket
        items.append({
            "ticket": p.ticket,
            "side": p.side.value,
            "volume": p.volume,
            "open_price": p.open_price,
            "current_price": price,
            "gross_profit": round(p.profit or 0.0, 2),
            "swap": round(p.swap, 2),
            "commission": round(p.commission, 2),
            "net_profit": round(p.net_profit, 2),
            "costs_known": p.costs_known,
            "open_time": opened_at.get(key),
        })
    items.sort(key=lambda i: i["net_profit"])

    net, gross, costs_known = engine._basket_pnl(positions)
    exit_cost = engine._estimated_exit_cost(positions)
    return {
        "connected": True,
        "positions": items,
        "pending_orders": len(pendings),
        "buy_stops": sum(1 for o in pendings if o.order_type == PendingType.BUY_STOP),
        "sell_stops": sum(1 for o in pendings if o.order_type == PendingType.SELL_STOP),
        "totals": {
            "count": len(items),
            "volume": round(sum(i["volume"] for i in items), 2),
            "gross_profit": gross,
            "net_profit": net,
            "estimated_exit_cost": exit_cost,
            # What the basket rule is actually judged on.
            "after_exit_cost": round(net - exit_cost, 2),
            "costs_known": costs_known,
            "target": engine.basket_take_profit_usd,
        },
    }


@router.get("/stats")
async def get_stats():
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
            equity_curve.append({"time": utc_iso(timestamp), "equity": round(running, 2)})

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
async def update_settings(body: SettingsUpdate):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        bot_manager.update_settings(updates)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return bot_manager.settings


@router.post("/mode")
async def set_mode(body: ModeUpdate):
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

    with db_module.SessionLocal() as session:
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
