"""Pending-order grid with persistent basket risk and an optional trained entry gate.

MT5 applies a separate disaster stop to each order. Basket exits still depend
on polling, broker execution and actual costs. These controls do not establish
that the grid or its classifier has a profitable edge.
"""

import asyncio
import logging
import threading
import time
from datetime import date, datetime, timezone
from typing import Callable, Optional

from zoneinfo import ZoneInfo

from app import db as db_module
from app.brokers.base import BrokerAdapter, PendingType, Position
from app.db import TradeRecord

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
        daily_profit_target_usd: float = 0.0,
        timezone_name: str = "Asia/Karachi",
        on_update: Optional[Callable[[dict], None]] = None,
    ):
        self._lock = threading.RLock()
        self._account_id = "legacy"
        self._risk_key = None
        self._closing_reason = None
        self._stop_after_close = False
        self.ai_gate = None
        self._last_history_sync = 0.0
        self._history_ready = False
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
        self.daily_profit_target_usd = daily_profit_target_usd
        self.timezone_name = timezone_name
        try:
            self._tz = ZoneInfo(timezone_name)
        except Exception:
            logger.warning("unknown timezone %r — falling back to UTC", timezone_name)
            self._tz = ZoneInfo("UTC")
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
        # The strict next-M1-candle gate. `_gate_anchor` is the candle stamp the
        # engine became ready to build on; no grid is placed until the broker
        # reports a strictly later one. `_gate_reason` is what armed it, so the
        # dashboard can say why it is waiting rather than looking stalled.
        self._gate_anchor = None
        self._gate_reason: str | None = None
        self._had_grid = False  # something of ours existed on the previous poll
        self._trading_day: str | None = None
        self._daily_target_hit = False
        self._daily_totals = None
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
        from app.config import settings
        if self.mode == "real" and not settings.allow_real_trading:
            raise PermissionError("Real trading disabled; complete broker tick and forward demo validation first")
        if self.mode == "real" and (self.basket_stop_loss_usd <= 0 or self.max_daily_loss_usd <= 0):
            raise PermissionError("Real trading requires positive basket and daily loss limits")
        if self.mode == "real" and not confirm_real:
            raise PermissionError("Starting on a REAL account requires explicit confirmation (confirm_real=true)")
        if not self.broker.is_connected():
            self.broker.connect()
        self._validate_account(self.broker.get_account_info())
        if not self._closing_reason:
            self._stop_after_close = False
        # Starting does not disturb anything already running. A basket that is
        # open, or a grid still resting, is picked back up and managed as it
        # was; the gate is only armed when a fresh grid would be needed.
        if not self._own_state_exists():
            self._arm_gate("Bot started")
        self._running = True
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if not self.broker.is_connected():
            self.broker.connect()
        self._stop_after_close = True
        self._closing_reason = "User requested stop"
        self._persist_risk()
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        try:
            while self._running and self._task is asyncio.current_task():
                try:
                    self._tick()
                except Exception as exc:
                    self._last_error = str(exc)
                    logger.exception("grid engine tick failed")
                await asyncio.sleep(self.poll_interval_seconds)
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------ tick

    def _tick(self) -> None:
        self._last_error = None
        account = self.broker.get_account_info()
        self._validate_account(account)
        positions = self.broker.get_open_positions(self.symbol, magic=self.magic_number)
        pendings = self.broker.get_pending_orders(self.symbol, magic=self.magic_number)
        if self._closing_reason:
            self._close_everything(positions, pendings, self._closing_reason)
            if self._closing_reason is None:
                self._arm_gate("Basket closed")
                if self._stop_after_close:
                    self._running = False
            self._broadcast_current()
            return

        candle = self._current_candle_time()
        accounting_ok = True
        try:
            self._roll_day(account.equity, candle)
            self._record_new_fills(positions, candle)
            self._settle_closed_trades()
            self._sync_broker_history()
        except Exception as exc:
            accounting_ok = False
            self._last_error = f"Accounting unavailable: {exc}; new grids blocked"
            logger.exception("Accounting synchronization failed; exits remain enabled")

        # A grid that was there last poll and is gone now, with nothing filled,
        # was removed outside the bot — deleted by hand in MT5, or expired. The
        # rebuild goes through the same next-candle gate as any other rebuild
        # rather than snapping back on the spot.
        if self._had_grid and not positions and not pendings and self._gate_anchor is None:
            self._arm_gate("Grid orders removed", candle)
        self._had_grid = bool(positions or pendings)

        basket_profit = round(sum(p.net_profit for p in positions), 2)
        hedged = self._is_hedged(positions) and not pendings
        if hedged and self._hedge_warned != len(positions):
            self._hedge_warned = len(positions)
            logger.warning(
                "basket has zero net volume: %d positions, reported profit %.2f, target %.2f. "
                "Directional price movement alone cannot recover it; spread, costs and stops still matter.",
                len(positions), basket_profit, self.basket_take_profit_usd,
            )
        elif not hedged:
            self._hedge_warned = None

        if self._check_risk_limits(account, positions, pendings):
            self._broadcast_current()
            return

        if positions:
            # The target is a floor, not a window: the spec is explicit that a
            # basket at 9.99 stays open and one at 10.00 or better closes.
            if all(p.costs_known for p in positions) and basket_profit >= self.basket_take_profit_usd:
                self._close_everything(positions, pendings, f"target reached (+{basket_profit:.2f})")
                if not self._closing_reason:
                    self._arm_gate("Basket closed")
                self._broadcast_current()
                return
            if self.basket_stop_loss_usd > 0 and basket_profit <= -self.basket_stop_loss_usd:
                self._close_everything(positions, pendings, f"basket stop hit ({basket_profit:.2f})")
                if not self._closing_reason:
                    self._arm_gate("Basket closed")
                self._broadcast_current()
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

        if not accounting_ok:
            self._broadcast(account, positions, pendings, basket_profit, note=self._last_error)
            return

        # The daily profit target. It is judged on settled trades for this
        # exact bot identity, so it reads the same after a restart as it did
        # before one, and clicking Start again cannot get past it.
        if self._check_daily_target(pendings):
            self._broadcast(
                account, positions, self._current_pendings(), basket_profit,
                note=(
                    f"Daily profit target reached: trading halted for this broker day "
                    f"(${self.daily_net():.2f} of ${self.daily_profit_target_usd:.2f})"
                ),
                hedged=hedged,
            )
            return

        if not self._within_session():
            self._broadcast(
                account, positions, pendings, basket_profit, note="outside trading session", hedged=hedged
            )
            return

        # A grid is rebuilt only when nothing at all is left of the last one.
        # Topping up a half-filled grid would keep adding exposure to a basket
        # that is already losing, which is not what the strategy says to do.
        if not positions and not pendings:
            unsettled = self._daily_totals.unsettled if self._daily_totals else 0
            if unsettled:
                self._broadcast(
                    account, positions, pendings, basket_profit,
                    note=(
                        f"Accounting pending: {unsettled} closed trade(s) have no realized result yet — "
                        "holding off on a new grid until the day's total is certain"
                    ),
                    hedged=hedged,
                )
                return
            ready, note = self._gate_status(candle)
            if not ready:
                self._broadcast(account, positions, pendings, basket_profit, note=note, hedged=hedged)
                return
            self._build_grid()
            pendings = self._current_pendings()

        self._broadcast(account, positions, pendings, basket_profit, hedged=hedged)

    # ------------------------------------------------------------------ grid

    def _build_grid(self) -> None:
        if self.buy_stop_levels + self.sell_stop_levels > self.max_open_positions:
            raise ValueError("Grid levels exceed position cap; lower levels before starting")
        if self.ai_gate and not self.ai_gate.allow(self.broker, self.symbol):
            self._last_error = self.ai_gate.reason
            return
        price = self.broker.get_current_price(self.symbol)
        info = self.broker.get_symbol_info(self.symbol)
        if hasattr(self.broker, "check_grid_margin"):
            self.broker.check_grid_margin(self.symbol, self.lot_size, self.buy_stop_levels, self.sell_stop_levels, price)
        # Stop orders have to clear the broker's minimum distance from the
        # market or the order is rejected outright, so the first level starts at
        # whichever is further: one grid step, or that minimum.
        if info.spread > 0.50:
            self._last_error = "Spread exceeds 0.50 price units; skipping grid"
            return
        first_step = max(self.grid_distance, info.min_stop_distance)
        self._reference_price = price
        placed_buy = placed_sell = 0

        direction = self.ai_gate.direction if self.ai_gate and self.ai_gate.mode == "filter" else "BOTH"
        for level in range(self.buy_stop_levels if direction in ("BOTH", "BUY") else 0):
            target = price + first_step + level * self.grid_distance
            try:
                self.broker.place_pending_order(
                    self.symbol, PendingType.BUY_STOP, self.lot_size, target, "BUY GRID", self.magic_number
                )
                placed_buy += 1
            except Exception:
                logger.exception("could not place BUY STOP at %.2f", target)

        for level in range(self.sell_stop_levels if direction in ("BOTH", "SELL") else 0):
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
        if ((direction in ("BOTH", "BUY") and placed_buy < self.buy_stop_levels)
                or (direction in ("BOTH", "SELL") and placed_sell < self.sell_stop_levels)):
            self._halt_reason = "Incomplete grid; flattening"
            self._closing_reason = self._halt_reason
            self._persist_risk()
            self._last_error = (
                f"only {placed_buy}/{self.buy_stop_levels} BUY and {placed_sell}/{self.sell_stop_levels} "
                f"SELL stops were accepted — check margin and the broker's minimum stop distance"
            )

    def _close_everything(self, positions: list[Position], pendings, reason: str) -> None:
        """Closes every position and cancels every pending order this bot owns,
        then verifies nothing survived. A leftover order from a finished basket
        would fill into the next one and corrupt its profit total."""
        self._closing_reason = reason
        self._persist_risk()
        logger.info("closing basket: %s", reason)
        realized = 0.0
        # Pull resting orders before closing positions.
        for order in pendings:
            try:
                self.broker.cancel_pending_order(order.ticket)
            except Exception:
                logger.exception("Cancellation failed; will retry")
        pendings = self._current_pendings()
        positions = self.broker.get_open_positions(self.symbol, magic=self.magic_number)
        try:
            self._record_new_fills(positions)
        except Exception:
            logger.exception("Could not record fills before closure; broker history will retry")
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
        try:
            self._settle_closed_trades()
        except Exception:
            logger.exception("Settlement unavailable after closure; will retry")

        leftover_positions = self.broker.get_open_positions(self.symbol, magic=self.magic_number)
        leftover_orders = self.broker.get_pending_orders(self.symbol, magic=self.magic_number)
        if leftover_positions or leftover_orders:
            try:
                self._record_new_fills(leftover_positions)
            except Exception:
                logger.exception("Could not record leftover fills; broker history will retry")
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
            try:
                self._settle_closed_trades()
            except Exception:
                logger.exception("Settlement unavailable after retry")

        still_there = self.broker.get_open_positions(self.symbol, magic=self.magic_number)
        if still_there:
            self._last_error = f"{len(still_there)} position(s) could not be closed — not starting a new grid"
            logger.error(self._last_error)
        if not still_there and not self._current_pendings():
            self._closing_reason = None
            if reason.startswith("target reached"):
                self._baskets_won += 1
            elif reason.startswith("basket stop hit"):
                self._baskets_stopped += 1
        self._persist_risk()
        self._last_basket_event = f"{reason}; settlement pending verification"
        self._reference_price = None
        # The day's realized total has just changed, so the daily target is
        # re-checked against settled trades before anything else is allowed.
        try:
            self._refresh_daily_totals()
        except Exception:
            logger.exception("Daily accounting update pending")

    def _is_hedged(self, positions: list[Position]) -> bool:
        """Zero net volume removes directional sensitivity, but costs can change."""
        if not positions:
            return False
        net = sum(p.volume if p.side.value == "BUY" else -p.volume for p in positions)
        smallest = min(p.volume for p in positions)
        return abs(net) < smallest / 2

    # ------------------------------------------------- next-M1-candle gate

    def _current_candle_time(self):
        """Timestamp of the M1 candle the broker is currently in, or None.

        None means the broker could not give a usable candle. Every caller
        treats that as "not allowed to place orders" rather than "carry on":
        the promise is that a grid never lands on the candle the engine became
        ready on, and that promise cannot be kept from a guess.
        """
        try:
            candles = self.broker.get_candles(self.symbol, "M1", 2)
        except Exception as exc:
            logger.warning("could not read the current M1 candle: %s", exc)
            return None
        if candles is None or len(candles) == 0:
            return None
        stamp = candles.index[-1]
        return stamp if stamp is not None else None

    def _arm_gate(self, reason: str, candle=None) -> None:
        """Start waiting for a candle strictly later than the current one.

        The anchor is written once. Re-arming on every poll would move the
        anchor forward each time and the wait would never end.
        """
        if self._gate_anchor is not None:
            return
        self._gate_anchor = candle if candle is not None else self._current_candle_time()
        self._gate_reason = reason
        if self._gate_anchor is None:
            logger.info("%s: waiting for a readable M1 candle before placing the grid", reason)
        else:
            logger.info("%s: waiting for the M1 candle after %s", reason, self._gate_anchor)

    def _gate_status(self, candle) -> tuple[bool, str | None]:
        """(may build, message). Fails closed on missing candle data."""
        if candle is None:
            reason = self._gate_reason or "Waiting"
            return False, f"{reason}: M1 candle data unavailable — not placing any orders until it returns"

        if self._gate_anchor is None:
            # Became ready without any event having armed the gate (a first
            # poll, or a gate cleared by a failed read). Anchor here so the
            # build still lands on a later candle.
            self._arm_gate(self._gate_reason or "Bot started", candle)

        if self._gate_anchor is not None and candle <= self._gate_anchor:
            reason = self._gate_reason or "Waiting"
            return False, f"{reason}: waiting for the next M1 candle before placing the grid"

        # Confirmed a later candle. Clearing here, before the build, is what
        # stops several polls inside the new candle each placing a grid.
        self._gate_anchor = None
        self._gate_reason = None
        return True, None

    def _own_state_exists(self) -> bool:
        """True when this bot already has positions or resting orders."""
        try:
            if self.broker.get_open_positions(self.symbol, magic=self.magic_number):
                return True
            return bool(self.broker.get_pending_orders(self.symbol, magic=self.magic_number))
        except Exception:
            logger.exception("could not read existing bot state")
            raise

    # ----------------------------------------------------- daily accounting

    def _trading_day_for(self, candle) -> str | None:
        """The broker trading day a candle belongs to, as YYYY-MM-DD.

        Taken from the broker's candle stamp, never from the machine clock, so
        the engine, the dashboard and the backtester all divide days the same
        way. Naive stamps are read as UTC and converted to the configured zone.
        """
        if candle is None:
            return None
        try:
            stamp = candle.to_pydatetime() if hasattr(candle, "to_pydatetime") else candle
        except Exception:
            return None
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(self._tz).date().isoformat()

    def _refresh_daily_totals(self):
        if self._trading_day is None:
            self._daily_totals = None
            return None
        self._daily_totals = db_module.daily_totals(self.symbol, self.mode, self.magic_number, self._trading_day, self._account_id)
        return self._daily_totals

    def daily_net(self) -> float:
        return self._daily_totals.net if self._daily_totals else 0.0

    def _check_daily_target(self, pendings) -> bool:
        """True when the day is locked. Blocks all order creation.

        The lock is derived from the settled trades themselves rather than
        stored as a flag, so it is exactly as true after a restart, a browser
        refresh or another Start click as it was before.
        """
        if self.daily_profit_target_usd <= 0:
            self._daily_target_hit = False
            return False
        totals = self._daily_totals or self._refresh_daily_totals()
        if totals is None:
            return False
        # Compared in whole cents so 9.99 does not slip through as 10.00 and
        # 10.00 is never rejected by a floating-point hair.
        reached = round(totals.net * 100) >= round(self.daily_profit_target_usd * 100)
        if not reached:
            self._daily_target_hit = False
            return False

        if not self._daily_target_hit:
            self._daily_target_hit = True
            logger.info(
                "daily profit target reached: %.2f of %.2f — halting for broker day %s",
                totals.net, self.daily_profit_target_usd, self._trading_day,
            )
        if pendings:
            for o in pendings:
                try:
                    self.broker.cancel_pending_order(o.ticket)
                except Exception:
                    logger.exception("failed to cancel pending order %s at the daily target", o.ticket)
            leftover = self._current_pendings()
            if leftover:
                self._last_error = (
                    f"{len(leftover)} pending order(s) survived the daily-target cancel — retrying"
                )
                logger.error(self._last_error)
            else:
                logger.info("all pending orders cancelled for the daily halt")
        return True

    def _current_pendings(self):
        return self.broker.get_pending_orders(self.symbol, magic=self.magic_number)

    def _validate_account(self, account):
        if account.trade_mode != self.mode or not account.hedging or account.currency != "USD":
            raise RuntimeError("Broker account must match the app mode and be a USD hedging account")
        if self._risk_key and account.account_id != self._account_id:
            raise RuntimeError("Broker account changed; restart with the correct account")
        if not self._risk_key:
            self._account_id = account.account_id
            self._risk_key = f"{account.account_id}|{self.symbol}|{self.magic_number}"
            saved = db_module.load_risk(self._risk_key)
            self._trading_day = saved.get("day")
            self._equity_peak = saved.get("peak", account.equity)
            self._halt_reason = saved.get("halt")
            self._closing_reason = self._closing_reason or saved.get("closing")
            self._stop_after_close = self._stop_after_close or saved.get("stop_after_close", False)

    def _persist_risk(self):
        if self._risk_key:
            db_module.save_risk(self._risk_key, {
                "day": self._trading_day, "peak": self._equity_peak,
                "halt": self._halt_reason, "closing": self._closing_reason,
                "stop_after_close": self._stop_after_close,
            })

    # ------------------------------------------------------------------ risk

    def _roll_day(self, equity: float, candle) -> None:
        """Rolls the day on the broker's own candle stamp, not the machine clock.

        On a rollover the daily counters and the daily-target lock reset, and
        the next-candle gate is armed so the first grid of the new day does not
        land on the rollover candle itself.
        """
        day = self._trading_day_for(candle)
        if day is not None and day != self._trading_day:
            first_day = self._trading_day is None
            self._trading_day = day
            self._day = None
            self._day_start_equity = equity
            self._day_realized = 0.0
            # Drawdown high-water mark survives midnight and restart.
            if self._halt_reason and self._halt_reason.startswith("daily loss"):
                self._halt_reason = None
            self._daily_target_hit = False
            if not first_day:
                logger.info("new broker trading day %s — daily counters and target lock reset", day)
                self._arm_gate("New trading day", candle)
        self._refresh_daily_totals()
        self._equity_peak = max(self._equity_peak, equity)
        self._persist_risk()

    def _check_risk_limits(self, account, positions, pendings) -> bool:
        """Check basket/account loss limits, including during history outages."""
        drawdown = 0.0
        if self._equity_peak > 0:
            drawdown = (self._equity_peak - account.equity) / self._equity_peak * 100

        try:
            self._refresh_daily_totals()
        except Exception:
            logger.exception("Using last known daily total; account and basket exits remain enabled")
        self._day_realized = self.daily_net()
        daily_with_floating = self._day_realized + sum(p.net_profit for p in positions)
        reason = None
        if self.max_daily_loss_usd > 0 and daily_with_floating <= -self.max_daily_loss_usd:
            reason = f"daily loss limit reached ({self._day_realized:.2f} of {-self.max_daily_loss_usd:.2f})"
        elif self.max_equity_drawdown_percent > 0 and drawdown >= self.max_equity_drawdown_percent:
            reason = f"equity drawdown {drawdown:.1f}% reached the {self.max_equity_drawdown_percent:.1f}% limit"

        reason = reason or self._halt_reason
        if reason is None:
            return False

        if self._halt_reason is None:
            self._halt_reason = reason
            self._persist_risk()
            logger.warning("risk protection activated: %s — flattening and standing down", reason)
        # Also recover a crash between persisting a halt and requesting closure.
        if positions or pendings:
            self._close_everything(positions, pendings, f"risk protection: {reason}")
        return True

    def _within_session(self) -> bool:
        if self.trading_start_hour == 0 and self.trading_end_hour >= 24:
            return True
        stamp = self._current_candle_time()
        if stamp is None:
            return False
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        hour = stamp.astimezone(timezone.utc).hour
        if self.trading_start_hour <= self.trading_end_hour:
            return self.trading_start_hour <= hour < self.trading_end_hour
        return hour >= self.trading_start_hour or hour < self.trading_end_hour  # window crosses midnight

    # ------------------------------------------------------- trade recording

    def _record_new_fills(self, positions: list[Position], candle=None) -> None:
        live = {p.identifier or p.ticket for p in positions}
        for p in positions:
            key = p.identifier or p.ticket
            if key in self._known_tickets:
                continue
            logger.info("%s STOP triggered: %s %.2f lots at %.2f", p.side.value, p.ticket, p.volume, p.open_price)
            with db_module.SessionLocal() as session:
                existing = session.query(TradeRecord).filter_by(
                    account_id=self._account_id, ticket=key
                ).filter(TradeRecord.status != "DUPLICATE").first()
                if existing:
                    self._known_tickets.add(key)
                    continue
                session.add(
                    TradeRecord(
                        account_id=self._account_id,
                        ticket=key, symbol=p.symbol, side=p.side.value, volume=p.volume,
                        open_price=p.open_price, sl=p.sl, tp=p.tp, mode=self.mode, status="OPEN",
                        open_time=datetime.fromisoformat(p.open_time),
                        magic=self.magic_number, trading_day=self._trading_day,
                    )
                )
                session.commit()
                self._known_tickets.add(key)
        gone = self._known_tickets - live
        if gone:
            self._settle_closed_trades()

    def _settle_closed_trades(self) -> None:
        """Marks tickets the broker no longer reports as open, using the broker's
        own realized figure so the dashboard matches the account history."""
        live = {p.identifier or p.ticket for p in self.broker.get_open_positions(self.symbol, magic=self.magic_number)}
        with db_module.SessionLocal() as session:
            pending = session.query(TradeRecord).filter(
                TradeRecord.account_id == self._account_id,
                TradeRecord.symbol == self.symbol, TradeRecord.magic == self.magic_number,
                (TradeRecord.status == "OPEN") | ((TradeRecord.status == "CLOSED") & TradeRecord.profit.is_(None)),
            ).all()
            closed = {r.ticket for r in pending} - live
        if not closed:
            return
        with db_module.SessionLocal() as session:
            for ticket in closed:
                record = session.query(TradeRecord).filter_by(account_id=self._account_id, ticket=ticket).filter(TradeRecord.status != "DUPLICATE").first()
                if record:
                    try:
                        profit = self.broker.get_realized_profit(ticket)
                    except Exception:
                        profit = None
                    record.status = "CLOSED"
                    record.close_time = datetime.now(timezone.utc)
                    record.profit = profit
                    record.magic = self.magic_number
                    # Stamped at settlement from the broker's candle, so the
                    # day a trade counts towards never shifts afterwards.
                    settled_at = self.broker.get_settlement_time(ticket) if hasattr(self.broker, "get_settlement_time") else None
                    record.close_time = settled_at or record.close_time
                    record.trading_day = self._trading_day_for(settled_at) if settled_at else self._trading_day
                self._known_tickets.discard(ticket)
            session.commit()
        self._refresh_daily_totals()

    def _sync_broker_history(self):
        if not hasattr(self.broker, "history_records") or (self._history_ready and time.monotonic() - self._last_history_sync < getattr(self.broker, "history_sync_interval", 30)):
            return
        rows = self.broker.history_records(self.symbol, self.magic_number)
        with db_module.SessionLocal() as session:
            for item in rows:
                record = session.query(TradeRecord).filter_by(
                    account_id=self._account_id, ticket=item["ticket"]
                ).filter(TradeRecord.status != "DUPLICATE").first()
                if record is None:
                    record = TradeRecord(account_id=self._account_id, mode=self.mode,
                                         magic=self.magic_number, sl=0, tp=0, **item)
                    session.add(record)
                else:
                    for name, value in item.items():
                        setattr(record, name, value)
                record.status = "CLOSED"
                record.trading_day = self._trading_day_for(item["close_time"])
            session.commit()
        if hasattr(self.broker, "acknowledge_history"):
            self.broker.acknowledge_history()
        self._history_ready = True
        self._last_history_sync = time.monotonic()
        self._refresh_daily_totals()

    # ------------------------------------------------------------- reporting

    def _broadcast_current(self):
        positions = self.broker.get_open_positions(self.symbol, magic=self.magic_number)
        pendings = self._current_pendings()
        self._broadcast(self.broker.get_account_info(), positions, pendings,
                        round(sum(p.net_profit for p in positions), 2),
                        hedged=self._is_hedged(positions) and not pendings)

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
                    "waiting_reason": note,
                    "trading_day": self._trading_day,
                    "daily_target": self.daily_profit_target_usd,
                    "daily_target_hit": self._daily_target_hit,
                    **self.daily_summary(),
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
            "closing": self._closing_reason,
            "account_id": self._account_id,
            "ai": self.ai_gate.status() if self.ai_gate else {"mode": "off"},
            "halted": self._halt_reason,
            "baskets_won": self._baskets_won,
            "baskets_stopped": self._baskets_stopped,
            "last_basket_event": self._last_basket_event,
            "timezone": self.timezone_name,
            "trading_day": self._trading_day,
            "daily_target": self.daily_profit_target_usd,
            "daily_target_hit": self._daily_target_hit,
            "waiting_for_candle": self._gate_anchor is not None or self._gate_reason is not None,
            "waiting_reason": self._gate_reason,
            **self.daily_summary(),
        }

    def daily_summary(self) -> dict:
        """The day's realized figures, from the one shared accounting source.

        The dashboard and the engine read the same function, so a card on
        screen cannot disagree with the number the halt was judged on. It
        resolves the trading day itself when asked while the bot is stopped,
        so the cards are right before the first tick as well as after it.
        """
        if not self._risk_key and self.broker.is_connected():
            self._validate_account(self.broker.get_account_info())
        totals = self._refresh_daily_totals()
        if totals is None:
            if self._trading_day is None:
                self._trading_day = self._trading_day_for(self._current_candle_time())
            totals = self._refresh_daily_totals()
        if totals is None:
            return {
                "today_gross_profit_usd": 0.0,
                "today_gross_loss_usd": 0.0,
                "today_net_profit_usd": 0.0,
                "today_unsettled_trades": 0,
            }
        return {
            "today_gross_profit_usd": totals.gross_profit,
            "today_gross_loss_usd": totals.gross_loss,
            "today_net_profit_usd": totals.net,
            "today_unsettled_trades": totals.unsettled,
        }
