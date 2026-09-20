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
        capital_reserve_percent: float = 50.0,
        on_update: Optional[Callable[[dict], None]] = None,
    ):
        self._account_id = "legacy"
        self._last_history_sync = 0.0
        self._history_ready = False
        self._profit_exit_reason = None
        self._profit_restart_pending = False
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
        # Share of the balance that must stay untouched by a single basket's
        # configured loss. It is what stops a small account from accepting a
        # loss budget it cannot survive.
        self.capital_reserve_percent = capital_reserve_percent
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
        # Startup, manual removal and loss exits use this next-M1 gate.
        # Profitable basket replacement bypasses it after confirmed closure.
        # `_gate_anchor` is the candle stamp the engine became ready on. `_gate_reason` is what armed it, so the
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
        # Why entries are refused right now, if they are. Distinct from
        # _halt_reason: a halt follows a breach, this is a gate that never let
        # the exposure be created in the first place.
        self._entry_block: str | None = None
        # Timeframe is fixed by the strategy; kept so the chart and the rest of
        # the app can ask the engine what it is running on.
        self.timeframe = "M1"
        # Read any unresolved halt straight away. A rebuilt engine — from a
        # restart, or from saving settings — must already know it is halted
        # before its first tick, and before the dashboard asks for status.
        self._restore_risk_state()

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
        # A loss halt is NOT cleared by starting. It was written to the database
        # when the limit was breached, and it stays until the exposure behind it
        # is reconciled and an owner explicitly clears it. Otherwise protection
        # would last exactly as long as the process did, and pressing Start
        # would be a way around it.
        self._restore_risk_state()
        # Starting does not disturb anything already running. A basket that is
        # open, or a grid still resting, is picked back up and managed as it
        # was; the gate is only armed when a fresh grid would be needed.
        if not self._own_state_exists():
            self._arm_gate("Bot started")
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
        self._bind_account(account)
        positions = self.broker.get_open_positions(self.symbol, magic=self.magic_number)
        pendings = self.broker.get_pending_orders(self.symbol, magic=self.magic_number)
        candle = self._current_candle_time()
        self._roll_day(account.equity, candle)
        self._record_new_fills(positions, candle)
        self._settle_closed_trades()
        self._sync_broker_history()

        # A grid that was there last poll and is gone now, with nothing filled,
        # was removed outside the bot — deleted by hand in MT5, or expired. The
        # rebuild goes through the same next-candle gate as any other rebuild
        # rather than snapping back on the spot.
        if self._had_grid and not positions and not pendings and self._gate_anchor is None and not self._profit_restart_pending:
            self._arm_gate("Grid orders removed", candle)
        self._had_grid = bool(positions or pendings)

        basket_net, basket_gross, costs_known = self._basket_pnl(positions)
        exit_cost = self._estimated_exit_cost(positions)
        # What the basket is conservatively worth if it were closed right now:
        # net of the costs already booked, minus what closing is still expected
        # to cost. The target is judged on this, so a basket is never closed on
        # a gross number the account will not actually receive.
        basket_profit = round(basket_net - exit_cost, 2)
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

        # Keep closing an earned profit cycle until the old basket is flat.
        # The replacement is built below in this same tick, without a candle gate.
        if self._profit_exit_reason or (positions and basket_profit >= self.basket_take_profit_usd):
            # costs_known is not required here: the estimate above is already
            # conservative, so an unknown cost can only delay a close, never
            # bring one forward.
            self._profit_exit_reason = self._profit_exit_reason or f"target reached (+{basket_profit:.2f})"
            self._close_everything(positions, pendings, self._profit_exit_reason)
            positions = self.broker.get_open_positions(self.symbol, magic=self.magic_number)
            pendings = self._current_pendings()
            account = self.broker.get_account_info()
            basket_net, basket_gross, costs_known = self._basket_pnl(positions)
            basket_profit = round(basket_net - self._estimated_exit_cost(positions), 2)
            if positions or pendings:
                self._broadcast(account, positions, pendings, basket_profit, note="Closing profitable basket")
                return
            self._baskets_won += 1
            self._profit_exit_reason = None
            self._profit_restart_pending = True
            self._gate_anchor = self._gate_reason = None
            self._had_grid = False
            hedged = False
            if self._check_risk_limits(account, positions, pendings):
                self._broadcast(account, positions, pendings, basket_profit)
                return

        if positions:
            # The loss side is judged on the WORSE of the two readings. A cost
            # the broker has not reported yet must never hold protection back.
            basket_loss_reading = min(basket_net, basket_gross) - exit_cost
            if self.basket_stop_loss_usd > 0 and basket_loss_reading <= -self.basket_stop_loss_usd:
                self._close_everything(positions, pendings, f"basket stop hit ({basket_profit:.2f})")
                self._baskets_stopped += 1
                self._arm_gate("Basket closed")
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
            if self._profit_restart_pending:
                ready, note = candle is not None, "Waiting for readable M1 data"
            else:
                ready, note = self._gate_status(candle)
            if not ready:
                self._broadcast(account, positions, pendings, basket_profit, note=note, hedged=hedged)
                return
            allowed, block = self._entry_gate(account)
            self._entry_block = None if allowed else block
            if not allowed:
                self._broadcast(account, positions, pendings, basket_profit, note=block, hedged=hedged)
                return
            self._build_grid()
            self._profit_restart_pending = False
            pendings = self._current_pendings()
            self._had_grid = bool(pendings)

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
        for order in pendings:
            try:
                self.broker.cancel_pending_order(order.ticket)
            except Exception:
                logger.exception("Pending cancellation will be retried")
        positions = self.broker.get_open_positions(self.symbol, magic=self.magic_number)
        self._record_new_fills(positions)
        pendings = self._current_pendings()
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
            self._record_new_fills(leftover_positions)
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
        # The day's realized total has just changed, so the daily target is
        # re-checked against settled trades before anything else is allowed.
        self._refresh_daily_totals()

    # --------------------------------------------------- money, honestly

    def _basket_pnl(self, positions: list[Position]) -> tuple[float, float, bool]:
        """(net, gross, every cost known).

        Gross is price movement alone. Net adds the swap and commission the
        broker has already booked. They differ by real money, and a basket
        closed on the gross number pays out less than the target promised.
        `costs_known` is False when any position could not report its costs.
        """
        gross = sum(p.profit or 0.0 for p in positions)
        net = sum(p.net_profit for p in positions)
        known = all(getattr(p, "costs_known", True) for p in positions)
        return round(net, 2), round(gross, 2), known

    def _estimated_exit_cost(self, positions: list[Position]) -> float:
        """What closing this basket is still expected to cost.

        Each position is closed on the far side of the spread, so the exit is
        charged at roughly half a spread per position. This is an estimate, not
        a quote: the real cost depends on the spread at the moment of closing,
        which can be far wider during news or a thin session.
        """
        if not positions:
            return 0.0
        try:
            info = self.broker.get_symbol_info(self.symbol)
        except Exception:
            return 0.0
        if not info.pip_size:
            return 0.0
        per_point = info.pip_value_per_lot
        half_spread_points = (info.spread / info.pip_size) / 2
        return round(sum(half_spread_points * p.volume * per_point for p in positions), 2)

    # ----------------------------------------------------- entry admission

    def _entry_gate(self, account) -> tuple[bool, str | None]:
        """Decides whether a NEW grid may be created at all.

        This runs before anything reaches the broker. Refusing here is the only
        protection that works on an account too small for the configured grid,
        because once the orders are resting the exposure already exists.
        """
        if self.basket_stop_loss_usd <= 0 and self.max_daily_loss_usd <= 0:
            return False, (
                "RISK_CONFIG_REQUIRED: no basket stop loss and no daily loss limit are set, so nothing "
                "would end a losing basket. Set at least one before the bot may open exposure."
            )

        affordable, reason = self._affordability(account)
        if not affordable:
            return False, reason
        return True, None

    def _grid_levels(self, price: float, info) -> tuple[list[float], list[float]]:
        """The exact prices _build_grid would use. Shared so the affordability
        check measures the grid that would really be placed, not an idealised
        one."""
        first_step = max(self.grid_distance, info.min_stop_distance)
        buys = [price + first_step + i * self.grid_distance for i in range(self.buy_stop_levels)]
        sells = [price - first_step - i * self.grid_distance for i in range(self.sell_stop_levels)]
        return buys, sells

    def _completed_grid_loss(self, price: float, info) -> float:
        """The loss a fully filled grid is already showing, as a positive number.

        Once both sides have filled, the buy and sell volumes cancel and the
        basket's profit stops responding to price at all: it is frozen at the
        sell entries minus the buy entries, less the spread paid to open every
        one of them. That number is always a loss, and no price recovers it.

        If it is larger than the basket stop, the configured grid cannot finish
        building without breaching the configured budget. That is a contradiction
        in the settings, not bad luck, and it is checkable before trading.
        """
        buys, sells = self._grid_levels(price, info)
        if not info.pip_size:
            return 0.0
        per_point, point = info.pip_value_per_lot, info.pip_size
        spread_points = info.spread / point
        frozen = 0.0
        for level in buys:  # a buy filled above the reference, closed back at it
            frozen += (level - price) / point * self.lot_size * per_point
        for level in sells:
            frozen += (price - level) / point * self.lot_size * per_point
        spread_cost = spread_points * self.lot_size * per_point * (len(buys) + len(sells))
        return round(frozen + spread_cost, 2)

    def _affordability(self, account) -> tuple[bool, str | None]:
        try:
            info = self.broker.get_symbol_info(self.symbol)
            price = self.broker.get_current_price(self.symbol)
        except Exception as exc:
            return False, f"Cannot price the grid: {exc}. No orders placed."

        balance = account.balance or 0.0
        if balance <= 0:
            return False, "Account balance is zero or unreadable — no orders placed."

        # 1. The loss budget must be something this balance can absorb while
        #    keeping the reserve the owner asked to protect.
        spendable = balance * max(0.0, 100.0 - self.capital_reserve_percent) / 100.0
        if self.basket_stop_loss_usd > 0 and self.basket_stop_loss_usd > spendable:
            return False, (
                f"NO_TRADE: the ${self.basket_stop_loss_usd:.2f} basket stop is more than the "
                f"${spendable:.2f} this ${balance:.2f} account may risk while keeping a "
                f"{self.capital_reserve_percent:.0f}% reserve."
            )

        # 2. The grid must be able to finish building inside that budget.
        frozen = self._completed_grid_loss(price, info)
        budget = self.basket_stop_loss_usd if self.basket_stop_loss_usd > 0 else spendable
        if frozen > budget:
            lots = (self.buy_stop_levels + self.sell_stop_levels) * self.lot_size
            return False, (
                f"NO_TRADE: a fully filled {self.buy_stop_levels}+{self.sell_stop_levels} grid at "
                f"{self.lot_size} lots ({lots:.2f} lots total) locks in about ${frozen:.2f} of loss once "
                f"both sides fill, which is more than the ${budget:.2f} budget. Reduce the levels, the "
                f"lot size or the spacing, or raise the budget — the grid as configured cannot finish "
                f"building without breaching it."
            )

        # 3. The broker must actually have the margin for it.
        free_margin = getattr(account, "free_margin", None)
        if free_margin is not None and free_margin <= 0:
            return False, "NO_TRADE: the account reports no free margin."
        return True, None

    # ------------------------------------------------- durable risk state

    def _risk_key(self) -> str:
        return f"halt:{self._account_id}:{self.symbol}:{self.magic_number}:{self.mode}"

    def _restore_risk_state(self) -> None:
        """Reads back a halt written by an earlier run of this same identity."""
        try:
            saved = db_module.load_risk(self._risk_key()) or {}
        except Exception:
            logger.exception("could not read the persisted risk state")
            return
        reason = saved.get("halt_reason")
        if reason:
            self._halt_reason = reason
            logger.warning("restored an unresolved risk halt: %s", reason)
        peak = saved.get("equity_peak")
        if isinstance(peak, (int, float)) and peak > self._equity_peak:
            self._equity_peak = float(peak)

    def _persist_risk_state(self) -> None:
        try:
            db_module.save_risk(
                self._risk_key(),
                {"halt_reason": self._halt_reason, "equity_peak": self._equity_peak},
            )
        except Exception:
            logger.exception("could not persist the risk state")

    def clear_halt(self) -> tuple[bool, str]:
        """Owner action. Refuses while exposure this bot owns is still open,
        because clearing a halt over live positions is how a breach becomes a
        bigger one."""
        try:
            positions = self.broker.get_open_positions(self.symbol, magic=self.magic_number)
            pendings = self.broker.get_pending_orders(self.symbol, magic=self.magic_number)
        except Exception as exc:
            return False, f"Cannot confirm the account is flat: {exc}"
        if positions or pendings:
            return False, (
                f"Not cleared: {len(positions)} position(s) and {len(pendings)} order(s) are still open. "
                "The halt stays until this bot's exposure is gone."
            )
        self._halt_reason = None
        self._persist_risk_state()
        return True, "Risk halt cleared."

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

    def _roll_day(self, equity: float, candle) -> None:
        """Rolls the day on the broker's own candle stamp, not the machine clock.

        On a rollover the daily counters and the daily-target lock reset, and
        the next-candle gate is armed so the first grid of the new day does not
        land on the rollover candle itself.
        """
        day = self._trading_day_for(candle)
        if day is not None and day != self._trading_day:
            # Binding to a day for the first time is not a rollover. A freshly
            # constructed engine goes through this on its very first tick, and
            # treating it as a new day is how a restart used to wipe its own
            # restored halt and its drawdown high-water mark.
            first_binding = self._trading_day is None
            self._trading_day = day
            self._day = None
            if not first_binding:
                self._day_start_equity = equity
                self._day_realized = 0.0
                self._daily_target_hit = False
                self._profit_restart_pending = False
                # A daily LOSS halt belongs to the day that produced it, so a
                # genuine rollover may release it — but only once this bot owns
                # nothing, because clearing a halt over live exposure is how one
                # breach becomes a larger one. A drawdown halt is not released
                # here at all: it needs an explicit owner reset.
                if self._halt_reason and self._halt_reason.startswith("daily loss"):
                    if self._own_state_exists():
                        logger.warning(
                            "new trading day %s but exposure is still open — the daily loss halt stands", day
                        )
                    else:
                        logger.info("new broker trading day %s — daily loss halt released", day)
                        self._halt_reason = None
                        self._persist_risk_state()
                logger.info("new broker trading day %s — daily counters reset", day)
                self._arm_gate("New trading day", candle)
        self._refresh_daily_totals()
        # The peak is a high-water mark across the whole life of the account for
        # this bot, not per day. Resetting it every morning would let an account
        # bleed down indefinitely, one "fresh" day at a time.
        if equity > self._equity_peak:
            self._equity_peak = equity
            self._persist_risk_state()

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
            if self._halt_reason is None:
                return False
            # Already halted and no longer breaching. The halt still stands, and
            # any exposure it was meant to remove is still chased below.
            reason = self._halt_reason

        if self._halt_reason is None:
            self._halt_reason = reason
            # Written down BEFORE the liquidation is attempted. If the process
            # dies mid-close, the next run still knows it was halted.
            self._persist_risk_state()
            logger.warning("risk protection activated: %s — flattening and standing down", reason)

        # The close is retried on every poll for as long as this bot still owns
        # anything. Closing once and then reporting "halted" forever is how a
        # breach turns into an unwatched open position: the bot looks stopped
        # while the money is still on the table.
        if positions or pendings:
            logger.warning(
                "risk halt still holds %d position(s) and %d order(s) — retrying closure",
                len(positions), len(pendings),
            )
            self._close_everything(positions, pendings, f"risk protection: {reason}")
        return True

    def _within_session(self) -> bool:
        """The trading window, read in the configured timezone.

        The hours are Pakistan time by default, because that is the clock the
        owner sets them by. Reading them as UTC instead silently shifted every
        window by five hours: "trade 17:00-22:00" became 22:00-03:00 PKT.

        The clock here is the machine's, not the broker's. A wrong machine
        clock moves the window, so this gate is a convenience, not a guarantee.
        """
        if self.trading_start_hour == 0 and self.trading_end_hour >= 24:
            return True
        hour = datetime.now(timezone.utc).astimezone(self._tz).hour
        if self.trading_start_hour <= self.trading_end_hour:
            return self.trading_start_hour <= hour < self.trading_end_hour
        return hour >= self.trading_start_hour or hour < self.trading_end_hour  # window crosses midnight

    def session_label(self) -> str:
        """How the window reads to the owner, in their own clock."""
        if self.trading_start_hour == 0 and self.trading_end_hour >= 24:
            return "always on"
        tz = self.timezone_name.split("/")[-1]
        return f"{self.trading_start_hour:02d}:00-{self.trading_end_hour:02d}:00 {tz}"

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

    def _bind_account(self, account):
        identity = getattr(account, "account_id", "legacy")
        if identity != self._account_id:
            self._account_id = identity
            self._known_tickets.clear()
            self._daily_totals = None
            self._history_ready = False
            # The halt is keyed by account, so binding to the real account is
            # the first moment its own halt can be read.
            self._restore_risk_state()

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
                        "open_time": p.open_time,
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
                    "entry_block_reason": self._entry_block,
                    "trading_window": self.session_label(),
                    "in_session": self._within_session(),
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
            "account_id": self._account_id,
            "profit_restart": "same_candle",
            "closing_profit_basket": bool(self._profit_exit_reason),
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
            # Why new exposure is refused right now, if it is. A halt follows a
            # breach; an entry block stopped the exposure being created at all.
            "entry_blocked": self._entry_block is not None,
            "entry_block_reason": self._entry_block,
            "trading_window": self.session_label(),
            "in_session": self._within_session(),
            "capital_reserve_percent": self.capital_reserve_percent,
            "basket_stop_loss_usd": self.basket_stop_loss_usd,
            "max_daily_loss_usd": self.max_daily_loss_usd,
            "max_equity_drawdown_percent": self.max_equity_drawdown_percent,
            **self.daily_summary(),
        }

    def daily_summary(self) -> dict:
        """The day's realized figures, from the one shared accounting source.

        The dashboard and the engine read the same function, so a card on
        screen cannot disagree with the number the halt was judged on. It
        resolves the trading day itself when asked while the bot is stopped,
        so the cards are right before the first tick as well as after it.
        """
        if self.broker.is_connected():
            self._bind_account(self.broker.get_account_info())
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
