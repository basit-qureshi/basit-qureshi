import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from app.brokers.base import BrokerAdapter, OrderSide, Position
from app.db import SessionLocal, TradeRecord
from app.risk.risk_manager import RiskManager
from app.strategy.ema_rsi_strategy import EmaRsiStrategy, Signal

logger = logging.getLogger("trading_engine")


class TradingEngine:
    """Ties broker + strategy + risk manager together in a poll loop.

    A strategy signal only ever reaches the broker after the risk manager
    approves it — the risk manager cannot be bypassed by the strategy.
    """

    def __init__(
        self,
        broker: BrokerAdapter,
        strategy: EmaRsiStrategy,
        risk_manager: RiskManager,
        symbol: str,
        timeframe: str,
        poll_interval_seconds: int,
        mode: str,
        on_update: Optional[Callable[[dict], None]] = None,
        max_trade_minutes: int = 0,
    ):
        self.broker = broker
        self.strategy = strategy
        self.risk_manager = risk_manager
        self.symbol = symbol
        self.timeframe = timeframe
        self.poll_interval_seconds = poll_interval_seconds
        self.mode = mode
        self.on_update = on_update
        self.max_trade_minutes = max_trade_minutes

        self._running = False
        self._task: asyncio.Task | None = None
        self._last_error: str | None = None
        self._last_known_profit: dict[str, float] = {}
        self._last_trade_candle_time = None

    @property
    def running(self) -> bool:
        return self._running

    def start(self, confirm_real: bool = False) -> None:
        if self._running:
            return
        if self.mode == "real" and not confirm_real:
            raise PermissionError("Starting on a REAL account requires explicit confirmation (confirm_real=true)")
        if not self.broker.is_connected():
            self.broker.connect()
        self._running = True
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        try:
            while self._running:
                try:
                    self._tick()
                    self._last_error = None
                except Exception as exc:
                    self._last_error = str(exc)
                    logger.exception("trading engine tick failed")
                await asyncio.sleep(self.poll_interval_seconds)
        except asyncio.CancelledError:
            pass

    def _tick(self) -> None:
        open_positions = self.broker.get_open_positions(self.symbol)
        account = self.broker.get_account_info()
        self._sync_closed_positions(open_positions)

        symbol_info = self.broker.get_symbol_info(self.symbol)
        current_price = self.broker.get_current_price(self.symbol)
        open_positions = self._close_stale_positions(open_positions)
        open_positions = self._apply_trailing_stop(
            open_positions, current_price, symbol_info.pip_size, symbol_info.min_stop_distance
        )

        decision = self.risk_manager.evaluate(
            balance=account.balance,
            equity=account.equity,
            open_trade_count=len(open_positions),
            symbol_info=symbol_info,
        )

        candles = self.broker.get_candles(self.symbol, self.timeframe, 200)
        # Strategies that confirm on a higher timeframe (e.g. breakout on M5,
        # entry on M1) declare it and get those candles passed in as well.
        higher_timeframe = getattr(self.strategy, "higher_timeframe", None)
        if higher_timeframe:
            higher_candles = self.broker.get_candles(self.symbol, higher_timeframe, 200)
            result = self.strategy.generate_signal(candles, higher_candles)
        else:
            result = self.strategy.generate_signal(candles)
        current_candle_time = candles.index[-1] if len(candles) else None

        # One entry per candle: after a stop-out, the same crossover keeps
        # "firing" on every poll until a new candle forms, which would
        # otherwise re-enter the same losing setup within seconds.
        already_traded_this_candle = (
            current_candle_time is not None and current_candle_time == self._last_trade_candle_time
        )

        if result.signal != Signal.NONE and decision.allowed and not already_traded_this_candle:
            side = OrderSide.BUY if result.signal == Signal.BUY else OrderSide.SELL
            sl, tp = self.risk_manager.compute_sl_tp(
                result.close, side.value, symbol_info.pip_size, symbol_info.min_stop_distance
            )
            # Size from the ACTUAL stop distance (which the broker's minimum may
            # have widened beyond stop_loss_pips) so the money at risk stays at
            # risk_percent — sizing from the configured pips while the real SL
            # sits further away would silently multiply the risk per trade.
            actual_stop_pips = abs(result.close - sl) / symbol_info.pip_size
            volume = self.risk_manager.calculate_position_size(account.balance, symbol_info, actual_stop_pips)
            position = self.broker.place_order(self.symbol, side, volume, sl, tp)
            self.risk_manager.record_trade_opened()
            self._last_trade_candle_time = current_candle_time
            self._log_new_trade(position)
            open_positions = open_positions + [position]

        for p in open_positions:
            self._last_known_profit[p.ticket] = p.profit

        self._broadcast(account, open_positions, result, decision)

    def _close_stale_positions(self, open_positions: list[Position]) -> list[Position]:
        """Closes trades that have been open past max_trade_minutes without
        reaching SL or TP. A scalping setup that hasn't resolved in that window
        has usually stopped being the setup that was entered, and the capital is
        better freed for the next one than left to drift into the stop."""
        if not self.max_trade_minutes:
            return open_positions

        now = datetime.now(timezone.utc)
        still_open = []
        for p in open_positions:
            try:
                opened = datetime.fromisoformat(p.open_time)
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                still_open.append(p)
                continue
            age_minutes = (now - opened).total_seconds() / 60
            if age_minutes >= self.max_trade_minutes:
                try:
                    self.broker.close_position(p.ticket)
                    logger.info("closed %s after %.1f minutes (time limit)", p.ticket, age_minutes)
                except Exception:
                    logger.exception("failed to time-close %s", p.ticket)
                    still_open.append(p)
            else:
                still_open.append(p)
        return still_open

    def _apply_trailing_stop(
        self, open_positions: list[Position], current_price: float, pip_size: float, min_stop_distance: float
    ) -> list[Position]:
        """Moves each open position's stop loss to breakeven/trailing once it
        qualifies (see RiskManager.compute_trailing_sl). Disabled entirely when
        breakeven_trigger_pips is 0 (the default)."""
        for p in open_positions:
            new_sl = self.risk_manager.compute_trailing_sl(
                p.side.value, p.open_price, p.sl, current_price, pip_size, min_stop_distance
            )
            if new_sl is not None:
                self.broker.modify_stop_loss(p.ticket, new_sl)
                p.sl = new_sl
                self._update_trade_sl(p.ticket, new_sl)
        return open_positions

    def _update_trade_sl(self, ticket: str, new_sl: float) -> None:
        with SessionLocal() as session:
            record = session.query(TradeRecord).filter_by(ticket=ticket, status="OPEN").first()
            if record:
                record.sl = new_sl
                session.commit()

    def _sync_closed_positions(self, current_open: list[Position]) -> None:
        """Any ticket we knew about last tick that is no longer open (SL/TP hit,
        or closed some other way) gets marked CLOSED in the DB with the broker's
        own recorded profit for that position (deal history on MT5), so what we
        display matches the broker's history exactly. Falls back to the last
        unrealized profit we observed if the broker can't report it.
        """
        current_tickets = {p.ticket for p in current_open}
        closed_tickets = set(self._last_known_profit.keys()) - current_tickets
        if not closed_tickets:
            return
        with SessionLocal() as session:
            for ticket in closed_tickets:
                record = session.query(TradeRecord).filter_by(ticket=ticket, status="OPEN").first()
                if record:
                    profit = None
                    try:
                        profit = self.broker.get_realized_profit(ticket)
                    except Exception:
                        logger.exception("failed to fetch realized profit for %s", ticket)
                    if profit is None:
                        profit = round(self._last_known_profit.get(ticket, 0.0), 2)
                    record.status = "CLOSED"
                    record.close_time = datetime.now(timezone.utc)
                    record.profit = profit
                self._last_known_profit.pop(ticket, None)
            session.commit()

    def _log_new_trade(self, position: Position) -> None:
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
                    mode=self.mode,
                    status="OPEN",
                )
            )
            session.commit()

    def _broadcast(self, account, open_positions: list[Position], result, decision) -> None:
        if not self.on_update:
            return
        payload = {
            "type": "tick",
            "balance": account.balance,
            "equity": account.equity,
            "open_positions": [
                {
                    "ticket": p.ticket,
                    "symbol": p.symbol,
                    "side": p.side.value,
                    "volume": p.volume,
                    "open_price": p.open_price,
                    "profit": p.profit,
                }
                for p in open_positions
            ],
            "signal": result.signal.value,
            "signal_reason": result.reason,
            "risk_allowed": decision.allowed,
            "risk_reason": decision.reason,
        }
        self.on_update(payload)

    def status(self) -> dict:
        return {
            "running": self._running,
            "mode": self.mode,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "connected": self.broker.is_connected(),
            "last_error": self._last_error,
        }
