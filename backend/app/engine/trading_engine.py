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
    ):
        self.broker = broker
        self.strategy = strategy
        self.risk_manager = risk_manager
        self.symbol = symbol
        self.timeframe = timeframe
        self.poll_interval_seconds = poll_interval_seconds
        self.mode = mode
        self.on_update = on_update

        self._running = False
        self._task: asyncio.Task | None = None
        self._last_error: str | None = None
        self._last_known_profit: dict[str, float] = {}

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
        balance_before = self.broker.get_account_info().balance
        # get_open_positions() is where SL/TP closures actually happen (broker-side),
        # so balance can change between these two get_account_info() calls.
        open_positions = self.broker.get_open_positions(self.symbol)
        account = self.broker.get_account_info()
        self._sync_closed_positions(open_positions, account.balance - balance_before)

        symbol_info = self.broker.get_symbol_info(self.symbol)
        current_price = self.broker.get_current_price(self.symbol)
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
        result = self.strategy.generate_signal(candles)

        if result.signal != Signal.NONE and decision.allowed:
            side = OrderSide.BUY if result.signal == Signal.BUY else OrderSide.SELL
            sl, tp = self.risk_manager.compute_sl_tp(
                result.close, side.value, symbol_info.pip_size, symbol_info.min_stop_distance
            )
            position = self.broker.place_order(self.symbol, side, decision.volume, sl, tp)
            self.risk_manager.record_trade_opened()
            self._log_new_trade(position)
            open_positions = open_positions + [position]

        for p in open_positions:
            self._last_known_profit[p.ticket] = p.profit

        self._broadcast(account, open_positions, result, decision)

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

    def _sync_closed_positions(self, current_open: list[Position], realized_delta: float) -> None:
        """Any ticket we knew about last tick that is no longer open (SL/TP hit,
        or closed some other way) gets marked CLOSED in the DB. The realized
        profit is the actual account balance delta since the last tick, not an
        estimate, so displayed stats match the broker's own numbers exactly.
        With multiple positions closing in the same tick, the delta is split
        evenly between them (an edge case that doesn't occur with the default
        max_open_trades=1).
        """
        current_tickets = {p.ticket for p in current_open}
        closed_tickets = set(self._last_known_profit.keys()) - current_tickets
        if not closed_tickets:
            return
        per_ticket_profit = round(realized_delta / len(closed_tickets), 2)
        with SessionLocal() as session:
            for ticket in closed_tickets:
                record = session.query(TradeRecord).filter_by(ticket=ticket, status="OPEN").first()
                if record:
                    record.status = "CLOSED"
                    record.close_time = datetime.now(timezone.utc)
                    record.profit = per_ticket_profit
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
