"""XAUUSD M1 pending-order grid.

The cycle, and nothing else:

    build a grid around the current price
      -> BuyStopLevels BUY STOPs above it, SellStopLevels SELL STOPs below,
         every one at the same fixed lot
    -> wait for stops to trigger into positions
    -> add up the NET profit of every position this bot opened
    -> the moment that total reaches BasketTakeProfitUSD, close every position
       and cancel every remaining pending order
    -> confirm nothing is left over, then build a fresh grid at the new price
    -> repeat

There are no entry filters, no indicators and no per-trade stop or target: a
grid position is exited only by the basket rule. That is the strategy as
specified, and adding anything else would make the thing being tested a
different strategy.

Understand what that leaves. There is no stop on an individual trade, so a
basket that never reaches its target simply keeps growing while price runs. At
0.01 lots a fully triggered 10+10 grid is 0.20 lots, which on gold is $20 of
profit or loss for every $1 the price moves - so an $18 move against a
one-sided grid is around $360. The only things that end a losing basket are
max_daily_loss_usd and max_equity_drawdown_percent, and the optional
basket_stop_loss_usd. They are the whole risk model, not decoration.
"""

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Callable, Optional

from app.brokers.base import BrokerAdapter, PendingType, Position
from app.db import SessionLocal, TradeRecord

logger = logging.getLogger("grid_engine")


class GridEngine:
    def __init__(
        self,
        broker: BrokerAdapter,
        symbol: str,
        mode: str,
        lot_size: float = 0.01,
        buy_stop_levels: int = 10,
        sell_stop_levels: int = 10,
        grid_distance: float = 0.30,
        basket_take_profit_usd: float = 10.0,
        basket_stop_loss_usd: float = 0.0,
        max_open_positions: int = 20,
        max_daily_loss_usd: float = 100.0,
        max_equity_drawdown_percent: float = 30.0,
        magic_number: int = 990022,
        trading_start_hour: int = 0,
        trading_end_hour: int = 24,
        poll_interval_seconds: int = 5,
        on_update: Optional[Callable[[dict], None]] = None,
    ):
        self.broker = broker
        self.symbol = symbol
        self.mode = mode
        self.lot_size = lot_size
        self.buy_stop_levels = buy_stop_levels
        self.sell_stop_levels = sell_stop_levels
        self.grid_distance = grid_distance
        self.basket_take_profit_usd = basket_take_profit_usd
        self.basket_stop_loss_usd = basket_stop_loss_usd
        self.max_open_positions = max_open_positions
        self.max_daily_loss_usd = max_daily_loss_usd
        self.max_equity_drawdown_percent = max_equity_drawdown_percent
        self.magic_number = magic_number
        self.trading_start_hour = trading_start_hour
        self.trading_end_hour = trading_end_hour
        self.poll_interval_seconds = poll_interval_seconds
        self.on_update = on_update

        self._running = False
        self._task: asyncio.Task | None = None
        self._last_error: str | None = None
        self._reference_price: float | None = None
        self._baskets_won = 0
        self._baskets_stopped = 0
        self._last_basket_event: str | None = None
        self._halt_reason: str | None = None
        self._known_tickets: set[str] = set()
        self._hedge_warned: int | None = None
        # The M1 candle a basket was closed on. The next grid waits for a candle
        # after it, so orders are never placed back onto the same candle whose
        # move just produced the profit.
        self._closed_on_candle = None
        self._day: date | None = None
        self._day_start_equity: float = 0.0
        self._day_realized: float = 0.0
        self._equity_peak: float = 0.0
        # Timeframe is fixed by the strategy; kept so the chart and the rest of
        # the app can ask the engine what it is running on.
        self.timeframe = "M1"

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
        self._halt_reason = None
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
                    logger.exception("grid engine tick failed")
                await asyncio.sleep(self.poll_interval_seconds)
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------ tick

    def _tick(self) -> None:
        account = self.broker.get_account_info()
        positions = self.broker.get_open_positions(self.symbol, magic=self.magic_number)
        pendings = self.broker.get_pending_orders(self.symbol, magic=self.magic_number)
        self._roll_day(account.equity)
        self._record_new_fills(positions)

        basket_profit = round(sum(p.profit or 0.0 for p in positions), 2)
        hedged = self._is_hedged(positions)
        if hedged and self._hedge_warned != len(positions):
            self._hedge_warned = len(positions)
            logger.warning(
                "basket is fully hedged: %d positions net to zero, so its profit is frozen at %.2f "
                "and the %.2f target can no longer be reached by any price. Only the basket stop, "
                "the daily loss limit or the drawdown limit will end it.",
                len(positions), basket_profit, self.basket_take_profit_usd,
            )
        elif not hedged:
            self._hedge_warned = None

        if self._check_risk_limits(account, positions, pendings):
            self._broadcast(account, positions, pendings, basket_profit, hedged=hedged)
            return

        if positions:
            # The target is a floor, not a window: the spec is explicit that a
            # basket at 9.99 stays open and one at 10.00 or better closes.
            if basket_profit >= self.basket_take_profit_usd:
                self._close_everything(positions, pendings, f"target reached (+{basket_profit:.2f})")
                self._baskets_won += 1
                account = self.broker.get_account_info()
                self._broadcast(account, [], self._current_pendings(), 0.0)
                return
            if self.basket_stop_loss_usd > 0 and basket_profit <= -self.basket_stop_loss_usd:
                self._close_everything(positions, pendings, f"basket stop hit ({basket_profit:.2f})")
                self._baskets_stopped += 1
                account = self.broker.get_account_info()
                self._broadcast(account, [], self._current_pendings(), 0.0)
                return

        # Cap exposure by pulling the rest of the grid once enough of it has
        # filled. Without this the limit would be a number in the settings that
        # nothing enforces, and the grid would keep adding lots regardless.
        if self.max_open_positions > 0 and len(positions) >= self.max_open_positions and pendings:
            logger.warning(
                "max open positions reached (%d) — cancelling the %d orders still resting",
                len(positions), len(pendings),
            )
            for o in pendings:
                try:
                    self.broker.cancel_pending_order(o.ticket)
                except Exception:
                    logger.exception("failed to cancel pending order %s", o.ticket)
            pendings = self._current_pendings()

        if not self._within_session():
            self._broadcast(
                account, positions, pendings, basket_profit, note="outside trading session", hedged=hedged
            )
            return

        # A grid is rebuilt only when nothing at all is left of the last one.
        # Topping up a half-filled grid would keep adding exposure to a basket
        # that is already losing, which is not what the strategy says to do.
        if not positions and not pendings:
            if self._waiting_for_next_candle():
                self._broadcast(
                    account, positions, pendings, basket_profit,
                    note="basket closed — waiting for the next M1 candle before placing the new grid",
                    hedged=hedged,
                )
                return
            self._build_grid()
            pendings = self._current_pendings()

        self._broadcast(account, positions, pendings, basket_profit, hedged=hedged)

    # ------------------------------------------------------------------ grid

    def _build_grid(self) -> None:
        price = self.broker.get_current_price(self.symbol)
        info = self.broker.get_symbol_info(self.symbol)
        # Stop orders have to clear the broker's minimum distance from the
        # market or the order is rejected outright, so the first level starts at
        # whichever is further: one grid step, or that minimum.
        first_step = max(self.grid_distance, info.min_stop_distance)
        self._reference_price = price
        placed_buy = placed_sell = 0

        for level in range(self.buy_stop_levels):
            target = price + first_step + level * self.grid_distance
            try:
                self.broker.place_pending_order(
                    self.symbol, PendingType.BUY_STOP, self.lot_size, target, "BUY GRID", self.magic_number
                )
                placed_buy += 1
            except Exception:
                logger.exception("could not place BUY STOP at %.2f", target)

        for level in range(self.sell_stop_levels):
            target = price - first_step - level * self.grid_distance
            try:
                self.broker.place_pending_order(
                    self.symbol, PendingType.SELL_STOP, self.lot_size, target, "SELL GRID", self.magic_number
                )
                placed_sell += 1
            except Exception:
                logger.exception("could not place SELL STOP at %.2f", target)

        logger.info(
            "grid created at %.2f: %d/%d BUY STOP, %d/%d SELL STOP, spacing %.2f, %.2f lots each",
            price, placed_buy, self.buy_stop_levels, placed_sell, self.sell_stop_levels,
            self.grid_distance, self.lot_size,
        )
        if placed_buy < self.buy_stop_levels or placed_sell < self.sell_stop_levels:
            self._last_error = (
                f"only {placed_buy}/{self.buy_stop_levels} BUY and {placed_sell}/{self.sell_stop_levels} "
                f"SELL stops were accepted — check margin and the broker's minimum stop distance"
            )

    def _close_everything(self, positions: list[Position], pendings, reason: str) -> None:
        """Closes every position and cancels every pending order this bot owns,
        then verifies nothing survived. A leftover order from a finished basket
        would fill into the next one and corrupt its profit total."""
        logger.info("closing basket: %s", reason)
        realized = 0.0
        for p in positions:
            try:
                realized += self.broker.close_position(p.ticket) or 0.0
            except Exception:
                logger.exception("failed to close position %s", p.ticket)
        for o in pendings:
            try:
                self.broker.cancel_pending_order(o.ticket)
            except Exception:
                logger.exception("failed to cancel pending order %s", o.ticket)

        self._day_realized += realized
        self._settle_closed_trades()

        leftover_positions = self.broker.get_open_positions(self.symbol, magic=self.magic_number)
        leftover_orders = self.broker.get_pending_orders(self.symbol, magic=self.magic_number)
        if leftover_positions or leftover_orders:
            # Retry once: a stop can fill in the moment between closing and
            # cancelling, which leaves a position the first pass never saw.
            for p in leftover_positions:
                try:
                    self.broker.close_position(p.ticket)
                except Exception:
                    logger.exception("failed to close leftover position %s", p.ticket)
            for o in leftover_orders:
                try:
                    self.broker.cancel_pending_order(o.ticket)
                except Exception:
                    logger.exception("failed to cancel leftover order %s", o.ticket)
            self._settle_closed_trades()

        still_there = self.broker.get_open_positions(self.symbol, magic=self.magic_number)
        if still_there:
            self._last_error = f"{len(still_there)} position(s) could not be closed — not starting a new grid"
            logger.error(self._last_error)
        self._last_basket_event = f"{reason}; realized {realized:+.2f}"
        self._reference_price = None
        self._closed_on_candle = self._current_candle_time()

    def _is_hedged(self, positions: list[Position]) -> bool:
        """True when the open positions net to zero volume.

        This is the dead end built into a two-sided grid, and it is worth
        naming plainly: with equal buy and sell volume the price terms cancel,
        so the basket's profit stops responding to price at all. It is frozen
        at the sum of the sell entries minus the buy entries, less the spread
        paid to open them — and since the buys filled above the reference and
        the sells below it, that frozen number is always a loss. A full 10+10
        grid at 0.30 spacing locks in about -$37.80 and no price, in either
        direction, ever recovers it. The take-profit target simply cannot be
        reached from here.
        """
        if not positions:
            return False
        net = sum(p.volume if p.side.value == "BUY" else -p.volume for p in positions)
        smallest = min(p.volume for p in positions)
        return abs(net) < smallest / 2

    def _current_candle_time(self):
        """Timestamp of the M1 candle price is currently in, or None if it can't
        be read. None is treated as "don't block" so a data hiccup can never
        leave the bot permanently unable to place a grid."""
        try:
            candles = self.broker.get_candles(self.symbol, "M1", 2)
        except Exception:
            logger.exception("could not read the current M1 candle")
            return None
        return candles.index[-1] if len(candles) else None

    def _waiting_for_next_candle(self) -> bool:
        """True while the new grid should be held back.

        A basket closes because price moved far enough on some candle. Placing
        the next grid on that same candle puts the fresh stop orders right into
        the move that just finished, so the rebuild waits for a new M1 candle
        to open first.
        """
        if self._closed_on_candle is None:
            return False
        now_candle = self._current_candle_time()
        if now_candle is None or now_candle > self._closed_on_candle:
            self._closed_on_candle = None
            return False
        return True

    def _current_pendings(self):
        try:
            return self.broker.get_pending_orders(self.symbol, magic=self.magic_number)
        except Exception:
            return []

    # ------------------------------------------------------------------ risk

    def _roll_day(self, equity: float) -> None:
        today = date.today()
        if self._day != today:
            self._day = today
            self._day_start_equity = equity
            self._day_realized = 0.0
            self._equity_peak = equity
            self._halt_reason = None
        self._equity_peak = max(self._equity_peak, equity)

    def _check_risk_limits(self, account, positions, pendings) -> bool:
        """The whole risk model. A grid has no per-trade stop, so if these do not
        fire nothing else will. Returns True when trading is halted."""
        drawdown = 0.0
        if self._equity_peak > 0:
            drawdown = (self._equity_peak - account.equity) / self._equity_peak * 100

        reason = None
        if self.max_daily_loss_usd > 0 and self._day_realized <= -self.max_daily_loss_usd:
            reason = f"daily loss limit reached ({self._day_realized:.2f} of {-self.max_daily_loss_usd:.2f})"
        elif self.max_equity_drawdown_percent > 0 and drawdown >= self.max_equity_drawdown_percent:
            reason = f"equity drawdown {drawdown:.1f}% reached the {self.max_equity_drawdown_percent:.1f}% limit"

        if reason is None:
            return self._halt_reason is not None

        if self._halt_reason is None:
            self._halt_reason = reason
            logger.warning("risk protection activated: %s — flattening and standing down", reason)
            # Standing down while positions stay open would leave the account
            # exposed with nothing watching it, so everything is closed first.
            self._close_everything(positions, pendings, f"risk protection: {reason}")
        return True

    def _within_session(self) -> bool:
        if self.trading_start_hour == 0 and self.trading_end_hour >= 24:
            return True
        hour = datetime.now(timezone.utc).hour
        if self.trading_start_hour <= self.trading_end_hour:
            return self.trading_start_hour <= hour < self.trading_end_hour
        return hour >= self.trading_start_hour or hour < self.trading_end_hour  # window crosses midnight

    # ------------------------------------------------------- trade recording

    def _record_new_fills(self, positions: list[Position]) -> None:
        live = {p.ticket for p in positions}
        for p in positions:
            if p.ticket in self._known_tickets:
                continue
            self._known_tickets.add(p.ticket)
            logger.info("%s STOP triggered: %s %.2f lots at %.2f", p.side.value, p.ticket, p.volume, p.open_price)
            with SessionLocal() as session:
                session.add(
                    TradeRecord(
                        ticket=p.ticket, symbol=p.symbol, side=p.side.value, volume=p.volume,
                        open_price=p.open_price, sl=p.sl, tp=p.tp, mode=self.mode, status="OPEN",
                    )
                )
                session.commit()
        gone = self._known_tickets - live
        if gone:
            self._settle_closed_trades()

    def _settle_closed_trades(self) -> None:
        """Marks tickets the broker no longer reports as open, using the broker's
        own realized figure so the dashboard matches the account history."""
        live = {p.ticket for p in self.broker.get_open_positions(self.symbol, magic=self.magic_number)}
        closed = self._known_tickets - live
        if not closed:
            return
        with SessionLocal() as session:
            for ticket in closed:
                record = session.query(TradeRecord).filter_by(ticket=ticket, status="OPEN").first()
                if record:
                    try:
                        profit = self.broker.get_realized_profit(ticket)
                    except Exception:
                        profit = None
                    record.status = "CLOSED"
                    record.close_time = datetime.now(timezone.utc)
                    record.profit = profit
                self._known_tickets.discard(ticket)
            session.commit()

    # ------------------------------------------------------------- reporting

    def _broadcast(
        self, account, positions, pendings, basket_profit: float, note: str | None = None,
        hedged: bool = False,
    ) -> None:
        if not self.on_update:
            return
        buys = sum(1 for o in pendings if o.order_type == PendingType.BUY_STOP)
        sells = len(pendings) - buys
        self.on_update(
            {
                "type": "tick",
                "balance": account.balance,
                "equity": account.equity,
                "open_positions": [
                    {
                        "ticket": p.ticket, "symbol": p.symbol, "side": p.side.value,
                        "volume": p.volume, "open_price": p.open_price, "profit": p.profit,
                    }
                    for p in positions
                ],
                "signal": "GRID",
                "signal_reason": note or self._last_basket_event or "grid running",
                "risk_allowed": self._halt_reason is None,
                "risk_reason": self._halt_reason or "ok",
                "grid": {
                    "reference_price": round(self._reference_price, 2) if self._reference_price else None,
                    "buy_stops": buys,
                    "sell_stops": sells,
                    "open_positions": len(positions),
                    "basket_profit": basket_profit,
                    "target": self.basket_take_profit_usd,
                    "baskets_won": self._baskets_won,
                    "baskets_stopped": self._baskets_stopped,
                    "last_event": self._last_basket_event,
                    "halted": self._halt_reason,
                    "hedged": hedged,
                },
            }
        )

    def status(self) -> dict:
        return {
            "running": self._running,
            "mode": self.mode,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "connected": self.broker.is_connected(),
            "last_error": self._last_error,
            "strategy_name": "GridEngine",
            "structural": False,
            "grid_mode": True,
            "halted": self._halt_reason,
            "baskets_won": self._baskets_won,
            "baskets_stopped": self._baskets_stopped,
            "last_basket_event": self._last_basket_event,
        }
