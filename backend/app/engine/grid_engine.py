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
from app.brokers.base import BrokerAdapter, OrderSide, PendingType, Position
from app.db import TradeRecord
from app.engine.lifecycle import (
    CAUSE_BASKET_STOP,
    CAUSE_DAILY_LOSS,
    CAUSE_DRAWDOWN,
    CAUSE_OWNER,
    CAUSE_PROFIT,
    STATE_CLOSING,
    STATE_DONE,
    STATE_RECONCILING,
    Admission,
    CloseIntent,
)
from app.engine.broker_owner import PROTECTIVE, REPORTING, BrokerOwner, SkipReporting
from app.engine.instrumentation import (
    QuoteObservation,
    Recorder,
    monotonic_ms,
    new_correlation_id,
)
from app.engine import grid_math
from app.engine.risk_accounting import build_day_risk
from app.evidence import session as evidence_session

logger = logging.getLogger("grid_engine")

# Bumped when the meaning of persisted risk state changes. A record written by
# an older version is migrated conservatively rather than trusted or discarded.
RISK_STATE_VERSION = 2


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
        capital_floor_usd: float = 0.0,
        protective_poll_seconds: float = 1.0,
        reporting_poll_seconds: float = 5.0,
        stall_after_ms: float = 4000.0,
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
        # The balance the account must never be traded down past. Separate from
        # the reserve above, which is a share of the balance set aside for one
        # proposed basket. 0 means the owner has not chosen one.
        self.capital_floor_usd = capital_floor_usd
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
        self._entry_unknowns: tuple[str, ...] = ()

        # --- close lifecycle ------------------------------------------------
        # A decision that was taken, not a condition that is currently true.
        # Once set it is driven to a broker-confirmed flat state; a price
        # recovery does not cancel it.
        self._close_intent: CloseIntent | None = None
        self._basket_id: str | None = None
        self._basket_seq = 0

        # --- owner controls -------------------------------------------------
        # Paused entries still manage and protect what is already open. This is
        # a different thing from stopping the management loop, and conflating
        # the two is how a stopped process looked like a flat account.
        self._entries_paused = False
        self._pause_reason: str | None = None

        # --- daily risk anchors ---------------------------------------------
        # Marked value of what this bot held when the trading day rolled. None
        # means no anchor could be established, which makes the day's reading
        # incomplete and blocks new exposure.
        self._day_open_marked: float | None = None
        # Last known net mark per ticket, so a position that has left the
        # broker's open list but has not settled yet keeps counting.
        self._last_marks: dict[str, float] = {}
        self._awaiting_settlement: set[str] = set()

        # A persistence failure must be visible and must block new entries.
        # Silently carrying on would mean the halt exists only in this process.
        self._persist_failed: str | None = None

        # Monotonic sequence so a consumer can discard an older snapshot that
        # arrives after a newer one.
        self._snapshot_seq = 0
        self._broadcast_failures = 0
        # Evidence capture is OPTIONAL and best-effort. NullEvidence makes
        # every call a no-op so the protective path needs no branch, and a
        # real recorder can never raise into it.
        self.evidence = evidence_session.NullEvidence()
        self._last_quote_evidence_ms = -1e12
        self._last_quote_evidence_count = -1
        self._last_heartbeat_ms = -1e12
        self._last_heartbeat_shape: tuple | None = None
        self._link_up: bool | None = None
        self._broker_trade_mode = "unchecked"
        # ticket -> the settlement event it was first recorded in, so a later
        # revision links a correction instead of rewriting the original.
        self._settlement_events: dict[str, tuple[str, float | None]] = {}
        # Set once the broker's own account classification has been checked
        # against the configured mode. Invalidated on any identity change.
        self._account_verified: str | None = None
        # Timeframe is fixed by the strategy; kept so the chart and the rest of
        # the app can ask the engine what it is running on.
        self.timeframe = "M1"
        # --- execution instrumentation and broker ownership -----------------
        # One owner for every broker call, so protective work can take priority
        # over a reporting read instead of queueing behind it.
        self._timing = Recorder()
        self._owner = BrokerOwner(broker, recorder=self._timing,
                                  stall_after_ms=stall_after_ms)
        self._last_quote: QuoteObservation | None = None
        self._last_symbol_info = None
        # Protection runs on its own cadence, independent of the M1 candle
        # boundary and of the dashboard's refresh. Reporting runs slower.
        self.protective_poll_seconds = protective_poll_seconds
        self.reporting_poll_seconds = reporting_poll_seconds
        self._protective_backoff = 0.0
        self._last_reporting_ms = 0.0
        self._reporting_overruns = 0

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

    def stop(self) -> tuple[bool, str]:
        """Stops the MANAGEMENT LOOP. This is not a flat account.

        Compatibility mapping: the old Stop button called exactly this and the
        UI reported "no new trades will be opened". That was true and badly
        incomplete — with the loop cancelled, the basket target, the basket
        stop, the daily limit and the drawdown limit are all no longer
        evaluated either, while the positions stay live at the broker.

        Entries are marked paused first, so a restart does not walk straight
        into a new grid over exposure nobody reconciled. The return value says
        what is actually left open; callers surface it rather than claiming the
        account is safe.
        """
        self._entries_paused = True
        self._pause_reason = self._pause_reason or "management loop stopped by owner"
        try:
            self._persist_risk_state()
        except Exception:  # pragma: no cover - _persist_risk_state already traps
            logger.exception("could not persist state while stopping")

        positions = self._safe_positions()
        pendings = self._safe_pendings()
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

        if positions is None or pendings is None:
            return False, (
                "Management stopped, but the broker could not be read — whether anything is still open "
                "is UNKNOWN. Check the terminal."
            )
        if positions or pendings:
            return False, (
                f"Management stopped. {len(positions)} position(s) and {len(pendings)} order(s) are STILL "
                "OPEN at the broker and are no longer being monitored by this bot. Nothing will close them."
            )
        return True, "Management stopped. The broker reports no positions or orders for this bot."

    async def _loop(self) -> None:
        """Protection on a fast cadence, reporting on a slow one.

        The old loop ran one combined tick every `poll_interval_seconds` and
        did reporting work in front of the risk check. Protection now runs on
        `protective_poll_seconds`, independent of both the M1 candle boundary
        and the dashboard refresh, and reporting runs at most every
        `reporting_poll_seconds` — and is skipped entirely, not queued, when it
        would sit in front of protective work.

        Backoff is bounded: an error doubles the interval up to eight times the
        configured value, so a broken terminal is retried steadily rather than
        hammered, and recovery restores the normal cadence immediately.
        """
        try:
            while self._running:
                cycle_started = monotonic_ms()
                try:
                    result = self._protective_tick()
                    self._last_error = None
                    self._protective_backoff = 0.0
                except Exception as exc:
                    result = None
                    self._last_error = str(exc)
                    logger.exception("protective tick failed")
                    self._protective_backoff = min(
                        max(self._protective_backoff * 2, self.protective_poll_seconds),
                        self.protective_poll_seconds * 8,
                    )

                try:
                    await self._maybe_report(result)
                except Exception:
                    logger.exception("reporting cycle failed")

                elapsed = (monotonic_ms() - cycle_started) / 1000.0
                self._timing.record("protective_cycle", monotonic_ms() - cycle_started)
                delay = max(0.0, (self._protective_backoff or self.protective_poll_seconds) - elapsed)
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            pass

    async def _maybe_report(self, protective_result) -> None:
        """Reporting work, bounded and never in front of protection.

        Runs at most once every `reporting_poll_seconds`, and is skipped while
        the broker owner is busy with protective work. A skipped cycle is
        counted, not queued: a backlog of stale reporting reads is worth less
        than the protective read it would delay.
        """
        now = monotonic_ms()
        if self._last_reporting_ms and (now - self._last_reporting_ms) < self.reporting_poll_seconds * 1000.0:
            return
        self._last_reporting_ms = now
        started = now
        try:
            self._reporting_tick(protective_result)
        except SkipReporting:
            self._reporting_overruns += 1
            logger.debug("reporting cycle stood aside for protective work")
        finally:
            self._timing.record("reporting_tick", monotonic_ms() - started)

    # ------------------------------------------------------------------ tick

    def _protective_tick(self) -> dict:
        """The decision path, and only the decision path.

        What a protective decision needs: whose account this is, what is open,
        what is resting, what it is worth, and whether an unfinished close is
        outstanding. What it does NOT need, and what used to run in front of it
        on every single tick: recording new fills to the database, sweeping the
        broker's settlement history, reading realised profit per closed ticket,
        preparing chart data and delivering a websocket frame.

        Under the synthetic benchmark those cost about 38 ms of a 63 ms median
        tick, and the history sweep alone is a stated 120 ms whenever its
        interval comes round. None of it can change whether a stop should fire,
        so none of it belongs before the check that fires it.

        Returns a small result the caller can broadcast; it does not broadcast.
        """
        cid = new_correlation_id("prot")
        with self._timing.span("protective_tick", cid):
            account = self._owner.account_info()
            self._bind_account(account)
            positions = self._owner.positions(self.symbol, self.magic_number)
            pendings = self._owner.pendings(self.symbol, self.magic_number)
            self._mark_positions(positions)
            self._note_quote(positions)
            self._ensure_day_bound(account)

            # An outstanding close is driven before anything else. The account
            # is already past a limit; nothing else competes with getting flat.
            if self._close_intent and not self._close_intent.finished:
                outstanding = self._drive_close_intent(positions, pendings)
                positions = self._safe_positions() or []
                pendings = self._safe_pendings() or []
                if not outstanding:
                    self._retire_finished_intent(self._close_intent) if self._close_intent else None
                return {
                    "correlation_id": cid, "account": account, "positions": positions,
                    "pendings": pendings, "closing": outstanding,
                }

            if self._check_risk_limits(account, positions, pendings):
                return {
                    "correlation_id": cid, "account": account,
                    "positions": self._safe_positions() or [], "pendings": self._safe_pendings() or [],
                    "closing": True,
                }

            basket_net, basket_gross, costs_known = self._basket_pnl(positions)
            exit_cost = self._estimated_exit_cost(positions)
            if self._profit_target_met(positions, basket_net, costs_known, exit_cost):
                self._profit_exit_reason = self._profit_exit_reason or (
                    f"target reached (+{round(basket_net - (exit_cost or 0.0), 2):.2f})"
                )
                self._open_close_intent(CAUSE_PROFIT, self._profit_exit_reason)
                outstanding = self._drive_close_intent(positions, pendings)
                if not outstanding and self._close_intent is not None:
                    self._retire_finished_intent(self._close_intent)
                return {
                    "correlation_id": cid, "account": account,
                    "positions": self._safe_positions() or [], "pendings": self._safe_pendings() or [],
                    "closing": outstanding,
                }

            # Capping exposure is protective work, not reporting: once enough
            # of the grid has filled, the orders still resting would keep
            # adding lots to a basket that is already the size it was allowed
            # to be. This belongs on the fast path with the stop, not behind a
            # history sweep.
            if self.max_open_positions > 0 and len(positions) >= self.max_open_positions and pendings:
                logger.warning(
                    "max open positions reached (%d) — cancelling the %d orders still resting",
                    len(positions), len(pendings),
                )
                for order in pendings:
                    try:
                        self.broker.cancel_pending_order(order.ticket)
                    except Exception:
                        logger.exception("failed to cancel pending order %s", order.ticket)
                pendings = self._safe_pendings() or []

            if self.basket_stop_loss_usd > 0 and positions:
                # The unverified exit reserve is not applied here; it would
                # bring the stop forward on an assumption nobody confirmed.
                loss_reading = min(basket_net, basket_gross)
                if loss_reading <= -self.basket_stop_loss_usd:
                    self._open_close_intent(CAUSE_BASKET_STOP, f"basket stop hit ({loss_reading:.2f})")
                    outstanding = self._drive_close_intent(positions, pendings)
                    self._arm_gate("Basket closed")
                    return {
                        "correlation_id": cid, "account": account,
                        "positions": self._safe_positions() or [], "pendings": self._safe_pendings() or [],
                        "closing": outstanding,
                    }

            return {
                "correlation_id": cid, "account": account, "positions": positions,
                "pendings": pendings, "closing": False,
            }

    def _reporting_tick(self, protective_result=None) -> None:
        """Everything a decision does not need: history, settlement, entries, UI.

        Entry admission lives here rather than on the protective path. Opening
        a new grid is not urgent — it can wait for the next reporting cycle —
        whereas closing one cannot. Nothing here can create exposure without
        going through the same `_entry_gate` as before.
        """
        account = (protective_result or {}).get("account") or self.broker.get_account_info()
        self._bind_account(account)
        candle = self._current_candle_time()
        self._roll_day(account.equity, candle)

        positions = self._safe_positions()
        pendings = self._safe_pendings()
        if positions is None or pendings is None:
            # Unreadable is not empty. Report it as unknown and try again.
            self._broadcast(account, positions, pendings, 0.0, note="broker state unreadable")
            return

        self._record_new_fills(positions, candle)
        self._settle_closed_trades()
        self._sync_broker_history()

        basket_net, basket_gross, costs_known = self._basket_pnl(positions)
        exit_cost = self._estimated_exit_cost(positions)
        basket_profit = round(basket_net - (exit_cost or 0.0), 2)
        hedged = self._is_hedged(positions)
        self._record_heartbeat(account, positions, pendings, basket_profit, costs_known)

        if self._close_intent and not self._close_intent.finished:
            self._broadcast(account, positions, pendings, basket_profit,
                            note=f"Closing: {self._close_intent.reason}", hedged=hedged)
            return
        if self._halt_reason:
            self._broadcast(account, positions, pendings, basket_profit, hedged=hedged)
            return

        self._consider_entry(account, positions, pendings, candle, basket_profit, hedged)

    #: Seconds between exposure heartbeats. Frequent enough that a reviewer can
    #: see what was open through a session, sparse enough not to bury decisions.
    HEARTBEAT_INTERVAL_S = 60.0

    def _record_heartbeat(self, account, positions, pendings, basket_profit, costs_known) -> None:
        """Remaining exposure, the day's marked risk, and measured cycle time.

        Answers the three questions a reviewer asks of a finished session that
        the decision events alone cannot: what was actually open at the time,
        what the day's risk reading was when it was open, and whether the
        protective loop was keeping up. The timing figures are measured spans
        from the monotonic clock, never a difference between two clocks.

        This is the state at the START of a reporting cycle. A grid placed
        later in the same cycle appears in its own GRID_PLACED event, with its
        own timestamp, and in the following heartbeat.
        """
        if getattr(self.evidence, "manifest", None) is None:
            return
        connected = True
        try:
            connected = bool(self.broker.is_connected())
        except Exception:
            connected = False
        if self._link_up is not None and connected != self._link_up:
            if connected:
                self._record_evidence(evidence_session.RECONNECT, link="up")
            else:
                self._note_evidence_gap("broker link reported down")
        self._link_up = connected

        now = monotonic_ms()
        shape = (len(positions), len(pendings), bool(self._halt_reason), bool(self._pause_reason))
        due = now - self._last_heartbeat_ms >= self.HEARTBEAT_INTERVAL_S * 1000
        if not due and shape == self._last_heartbeat_shape:
            return
        # A change in what is open is recorded when it happens, not up to a
        # minute later: "nothing was open" for a period when something was is
        # the one thing this record must never say.
        self._last_heartbeat_ms = now
        self._last_heartbeat_shape = shape
        try:
            risk = self._day_risk(positions).as_dict()
        except Exception:
            risk = None
        self._record_evidence(
            evidence_session.EXPOSURE_SNAPSHOT,
            basket_id=self._basket_id,
            positions_open=len(positions), orders_resting=len(pendings),
            basket_profit=basket_profit, costs_known=costs_known,
            equity=getattr(account, "equity", None),
            balance=getattr(account, "balance", None),
            day_risk=risk, halted=self._halt_reason, paused=self._pause_reason,
            link_up=connected,
            protective_cycle_ms=self._timing.stats("protective_tick").as_dict(),
        )

    def _consider_entry(self, account, positions, pendings, candle, basket_profit, hedged) -> None:
        """The entry path, unchanged in policy and moved off the fast loop."""
        # The daily target is checked BEFORE the "something is already resting"
        # return, because reaching it has to cancel the orders still out there.
        # Checking it only on an empty book would leave a full grid resting
        # through a halt that claims trading has stopped for the day.
        if self._check_daily_target(pendings):
            self._broadcast(account, positions, self._current_pendings(), basket_profit,
                            note=(f"Daily profit target reached: trading halted for this broker day "
                                  f"(${self.daily_net():.2f} of ${self.daily_profit_target_usd:.2f})"),
                            hedged=hedged)
            return
        if not self._within_session():
            self._broadcast(account, positions, pendings, basket_profit,
                            note="outside trading session", hedged=hedged)
            return
        if positions or pendings:
            self._broadcast(account, positions, pendings, basket_profit, hedged=hedged)
            return
        unsettled = self._daily_totals.unsettled if self._daily_totals else 0
        if unsettled:
            self._broadcast(account, positions, pendings, basket_profit,
                            note=(f"Accounting pending: {unsettled} closed trade(s) have no realized "
                                  "result yet — holding off on a new grid until the day's total is certain"),
                            hedged=hedged)
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
        self._record_evidence(evidence_session.ADMISSION_DECIDED, allowed=allowed,
                             reason=block or "all gates passed",
                             unknowns=list(self._entry_unknowns))
        if not allowed:
            self._broadcast(account, positions, pendings, basket_profit, note=block, hedged=hedged)
            return
        self._build_grid()
        self._record_evidence(evidence_session.GRID_PLACED, basket_id=self._basket_id,
                             reference_price=self._reference_price,
                             orders_resting=len(self._current_pendings()),
                             lot=self.lot_size, spacing=self.grid_distance,
                             buy_levels=self.buy_stop_levels,
                             sell_levels=self.sell_stop_levels)
        self._profit_restart_pending = False
        pendings = self._current_pendings()
        self._had_grid = bool(pendings)
        self._broadcast(account, positions, pendings, basket_profit, hedged=hedged)

    def _ensure_day_bound(self, account) -> None:
        """Binds the trading day if nothing has yet.

        The day rolls once a day, so rolling it belongs on the reporting
        cadence — but the daily loss limit cannot be judged without it. This
        pays the one candle read needed after a restart and then stays out of
        the way.
        """
        if self._trading_day is not None:
            return
        try:
            candle = self._current_candle_time()
        except Exception:
            return
        self._roll_day(account.equity, candle)

    def _note_quote(self, positions) -> None:
        """Records the current quote with its two clocks kept apart.

        MT5's symbol_info_tick returns the LAST tick, not a stream of every
        tick, and can return None. Polling therefore cannot be claimed to
        observe every transient move; a missing quote is recorded as missing.
        """
        try:
            info = self._owner.symbol_info(self.symbol)
            price = self.broker.get_current_price(self.symbol)
        except Exception:
            self._last_quote = QuoteObservation(price=None, broker_time=None, missing=True)
            return
        self._last_quote = QuoteObservation(
            price=price, broker_time=None, missing=price is None,
        )
        self._last_symbol_info = info
        self._record_quote_evidence(price, info, positions)

    #: Seconds between recorded quotes while nothing is changing. One quote per
    #: protective tick would be ~86k events a day and would push the decisions
    #: worth reading out of a bounded buffer. A change in how many positions are
    #: open, or a missing quote, is recorded immediately regardless.
    QUOTE_EVIDENCE_INTERVAL_S = 15.0

    def _record_quote_evidence(self, price, info, positions) -> None:
        if getattr(self.evidence, "manifest", None) is None:
            return                      # no session recording; nothing to throttle
        now = monotonic_ms()
        changed = len(positions) != self._last_quote_evidence_count
        due = (now - self._last_quote_evidence_ms) >= self.QUOTE_EVIDENCE_INTERVAL_S * 1000
        if not (changed or due or price is None):
            return
        self._last_quote_evidence_ms = now
        self._last_quote_evidence_count = len(positions)
        self._record_evidence(evidence_session.QUOTE_OBSERVED, price=price,
                              spread=getattr(info, "spread", None),
                              missing=price is None,
                              costs_known=all(getattr(p, "costs_known", True) for p in positions),
                              positions_open=len(positions),
                              sampled_every_seconds=self.QUOTE_EVIDENCE_INTERVAL_S)

    def _tick(self) -> None:
        """One combined cycle: protection, then reporting.

        The loop runs these two halves on DIFFERENT cadences — that separation
        is the point of Phase B. `_tick` keeps them composed in one call so the
        whole regression suite exercises the same code the loop runs, rather
        than a second implementation that only tests see.
        """
        result = self._protective_tick()
        self._reporting_tick(result)

    def _build_grid(self) -> None:
        price = self.broker.get_current_price(self.symbol)
        info = self.broker.get_symbol_info(self.symbol)
        # Stop orders have to clear the broker's minimum distance from the
        # market or the order is rejected outright, so the first level starts at
        # whichever is further: one grid step, or that minimum. The levels come
        # from the SAME function the admission estimate uses — two copies of this
        # arithmetic would let the gate measure a grid the engine does not place.
        buy_targets, sell_targets = self._grid_levels(price, info)
        self._reference_price = price
        placed_buy = placed_sell = 0

        for target in buy_targets:
            try:
                self.broker.place_pending_order(
                    self.symbol, PendingType.BUY_STOP, self.lot_size, target, "BUY GRID", self.magic_number
                )
                placed_buy += 1
            except Exception:
                logger.exception("could not place BUY STOP at %.2f", target)

        for target in sell_targets:
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

    # ------------------------------------------------- close lifecycle

    def _record_evidence(self, kind: str, **payload) -> None:
        """Records one event, and cannot fail the caller.

        The real recorder already swallows its own errors, but the engine does
        not get to depend on that: evidence capture is optional and a swapped-in
        recorder must never be able to break a protective cycle.
        """
        try:
            self.evidence.record(kind, **payload)
        except Exception:
            logger.exception("evidence capture failed (continuing)")

    def _note_evidence_gap(self, reason: str, **payload) -> None:
        try:
            self.evidence.note_gap(reason, **payload)
        except Exception:
            logger.exception("evidence gap capture failed (continuing)")

    def _new_basket_id(self) -> str:
        self._basket_seq += 1
        return f"{self._account_id}:{self.symbol}:{self.magic_number}:{self._basket_seq}"

    def _open_close_intent(self, cause: str, reason: str) -> CloseIntent:
        """Records the decision to flatten, before anything is sent.

        Persisted first so a process that dies mid-close is picked up by the
        next run. If the write fails the intent still stands in memory and the
        failure is surfaced — best-effort protection continues either way.
        """
        if self._close_intent and not self._close_intent.finished:
            return self._close_intent
        self._basket_id = self._basket_id or self._new_basket_id()
        self._close_intent = CloseIntent(cause=cause, reason=reason, basket_id=self._basket_id)
        self._persist_risk_state()
        self._record_evidence(evidence_session.CLOSE_INTENT_OPENED,
                             basket_id=self._basket_id, cause=cause, reason=reason,
                             latches_entries=self._close_intent.latches_entries)
        logger.warning("close intent opened (%s): %s", cause, reason)
        return self._close_intent

    def _drive_close_intent(self, positions, pendings) -> bool:
        """Pushes an open close intent toward broker-confirmed flat.

        Returns True while the intent is still outstanding. A price recovery
        does not retire it: only the broker reporting no positions and no
        resting orders does. That is the difference between 'the condition that
        fired is no longer true' and 'the exposure is actually gone'.
        """
        intent = self._close_intent
        if intent is None or intent.finished:
            return False

        intent.attempts += 1
        if positions or pendings:
            intent.state = STATE_CLOSING
            logger.warning(
                "%s: still holding %d position(s) and %d order(s) — attempt %d",
                intent.reason, len(positions), len(pendings), intent.attempts,
            )
            self._close_everything(positions, pendings, intent.reason)
            # Re-read rather than assume. Whether it worked is the broker's
            # answer, not ours.
            positions = self._safe_positions()
            pendings = self._safe_pendings()

        if positions is None or pendings is None:
            # Could not confirm. Staying in RECONCILING is the honest state:
            # exposure is not proven gone, so nothing new may be opened.
            intent.state = STATE_RECONCILING
            intent.last_error = "broker state could not be read — exposure not confirmed gone"
            self._persist_risk_state()
            self._note_evidence_gap("broker state unreadable during an open close intent",
                                   basket_id=intent.basket_id, confirmed_flat=False)
            return True

        if positions or pendings:
            intent.state = STATE_RECONCILING
            intent.last_error = (
                f"{len(positions)} position(s) and {len(pendings)} order(s) survived the close"
            )
            self._persist_risk_state()
            self._record_evidence(evidence_session.CLOSE_INTENT_PROGRESS,
                                 basket_id=intent.basket_id, state=intent.state,
                                 attempts=intent.attempts,
                                 positions_remaining=len(positions),
                                 orders_remaining=len(pendings),
                                 confirmed_flat=False, last_error=intent.last_error)
            return True

        intent.state = STATE_DONE
        intent.last_error = None
        self._record_evidence(evidence_session.CLOSE_INTENT_DONE,
                             basket_id=intent.basket_id, cause=intent.cause,
                             attempts=intent.attempts, confirmed_flat=True)
        if not intent.counted:
            # Counted once for the basket, not once per retry.
            intent.counted = True
            if intent.cause == CAUSE_PROFIT:
                self._baskets_won += 1
            elif intent.cause == CAUSE_BASKET_STOP:
                self._baskets_stopped += 1
        if intent.latches_entries:
            # A loss exit does not quietly rebuild. Resuming is an owner action.
            self._entries_paused = True
            self._pause_reason = f"entries paused after {intent.cause}: {intent.reason}"
            logger.warning("entries latched: %s", self._pause_reason)
        self._basket_id = None
        logger.info("close intent complete (%s) after %d attempt(s)", intent.cause, intent.attempts)
        self._persist_risk_state()
        # Retired here, not only by the caller that happens to remember. Only
        # the profit path used to retire, so after a stop or a risk halt a
        # DONE intent stayed attached for good — and `_check_risk_limits`
        # re-opens a close for live exposure only when NO intent is attached.
        # A halted engine therefore ignored exposure that appeared after its
        # own close confirmed, which is the one state it must never ignore.
        self._retire_finished_intent(intent)
        return False

    def _retire_finished_intent(self, intent: CloseIntent) -> None:
        """Clears a completed intent exactly once.

        The counting already happened in `_drive_close_intent`; this only
        releases the slot and restores the path the cause allows. A profitable
        close is not a latching cause, so it re-enables same-candle replacement;
        a loss close leaves the entries paused it set.
        """
        if self._close_intent is not intent and self._close_intent is not None:
            return
        self._close_intent = None
        if intent.cause == CAUSE_PROFIT:
            self._profit_exit_reason = None
            self._profit_restart_pending = True
            self._gate_anchor = self._gate_reason = None
            self._had_grid = False
        self._persist_risk_state()

    def _safe_positions(self):
        try:
            return self.broker.get_open_positions(self.symbol, magic=self.magic_number)
        except Exception:
            logger.exception("could not read open positions")
            return None

    def _safe_pendings(self):
        try:
            return self.broker.get_pending_orders(self.symbol, magic=self.magic_number)
        except Exception:
            logger.exception("could not read pending orders")
            return None

    # --------------------------------------------------------- owner actions

    def pause_entries(self, reason: str = "paused by owner") -> tuple[bool, str]:
        """Stops new exposure while continuing to manage and protect what is
        open. The management loop keeps running — this is not Stop."""
        self._entries_paused = True
        self._pause_reason = reason
        pendings = self._safe_pendings()
        cancelled = 0
        if pendings:
            # Resting entry orders are exposure waiting to happen, so they go.
            for order in pendings:
                try:
                    self.broker.cancel_pending_order(order.ticket)
                    cancelled += 1
                except Exception:
                    logger.exception("could not cancel %s while pausing entries", order.ticket)
            # A stop can fill in the moment between reading and cancelling, so
            # the result is re-read rather than assumed.
            self._record_new_fills(self._safe_positions() or [])
        self._persist_risk_state()
        self._record_evidence(evidence_session.PAUSE, reason=reason,
                              orders_cancelled=cancelled)
        return True, f"Entries paused. {cancelled} resting order(s) cancelled; open positions are still managed."

    def resume_entries(self) -> tuple[bool, str]:
        """Owner action. Refuses while a halt or an unfinished close stands."""
        if self._halt_reason:
            return False, f"Not resumed: a risk halt is still active — {self._halt_reason}"
        if self._close_intent and not self._close_intent.finished:
            return False, f"Not resumed: a close is still in progress ({self._close_intent.state})"
        if self._persist_failed:
            return False, f"Not resumed: {self._persist_failed}"
        self._entries_paused = False
        self._pause_reason = None
        self._close_intent = None
        self._persist_risk_state()
        self._record_evidence(evidence_session.RESUME, by="owner")
        return True, "Entries resumed. All current risk checks still apply before any grid is placed."

    def close_and_pause(self, reason: str = "closed by owner") -> tuple[bool, str]:
        """Flatten this bot's own exposure and stop opening more.

        Only positions carrying this bot's magic number are touched; a manual
        trade is never closed by this. It stays active until the broker
        confirms flat, so it is not a fire-and-forget button.
        """
        self._entries_paused = True
        self._pause_reason = reason
        self._open_close_intent(CAUSE_OWNER, reason)
        outstanding = self._drive_close_intent(self._safe_positions() or [], self._safe_pendings() or [])
        if outstanding:
            return True, "Closing. This stays active until the broker confirms no positions or orders remain."
        return True, "Closed and paused. The broker reports no positions or orders for this bot."

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
        # Guarded: an unreadable broker here must leave the intent outstanding,
        # not raise out of the close path. None means "could not confirm", and
        # the caller keeps the intent open on that basis.
        positions = self._safe_positions()
        if positions is None:
            self._note_evidence_gap("broker unreadable while closing — exposure not confirmed gone")
            self._last_error = "broker unreadable during a close — exposure not confirmed gone"
            return
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
        self._settle_closed_trades(reason)

        leftover_positions = self._safe_positions()
        leftover_orders = self._safe_pendings()
        if leftover_positions is None or leftover_orders is None:
            self._note_evidence_gap("broker unreadable while verifying a close")
            self._last_error = "broker unreadable during a close — exposure not confirmed gone"
            return
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
            self._settle_closed_trades(reason)

        still_there = self._safe_positions()
        if still_there is None:
            self._note_evidence_gap("broker unreadable after a close attempt")
            self._last_error = "broker unreadable after a close — exposure not confirmed gone"
            return
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

    def _basket_value(self, positions: list[Position]) -> float:
        """The basket's conservative value, for display and broadcast.

        One contract shared by the engine, the API and the UI, so a card and a
        decision can never be computed two different ways.
        """
        net, _gross, _known = self._basket_pnl(positions)
        reserve = self._estimated_exit_cost(positions)
        return round(net - (reserve or 0.0), 2)

    def _profit_target_met(self, positions, basket_net: float, costs_known: bool, exit_cost) -> bool:
        """Whether the basket has genuinely earned the target.

        Three things have to be true, and an unknown is never one of them:

        * there is something to close;
        * every position's costs were reported, because a basket whose
          commission came back as an unreported 0.00 has not been shown to have
          cleared anything;
        * the exit reserve was estimable.

        The comparison is made on unrounded values against the target. Rounding
        first is how 9.995 gets displayed as 10.00 and then treated as though it
        had crossed.
        """
        if not positions:
            return False
        if not costs_known:
            logger.info("target not judged: the broker has not reported costs for every position")
            return False
        if exit_cost is None:
            logger.info("target not judged: the exit cost could not be estimated")
            return False
        return (basket_net - exit_cost) >= self.basket_take_profit_usd

    def _estimated_exit_cost(self, positions: list[Position]) -> float | None:
        """What closing this basket is still expected to cost, or None.

        None means "not estimable right now" and is deliberately NOT zero.
        Returning zero on a failed symbol read was the old behaviour and it
        failed open: exactly when data was bad, the reading quietly became less
        conservative.

        Double counting. If the broker's floating profit is already struck at
        the executable closing side - which MT5's is understood to be - then a
        further half-spread per position charges the exit twice. That semantic
        is not verified for this adapter, so `SymbolInfo.profit_includes_exit_spread`
        stays None and the reserve is treated as UNVERIFIED: it tightens the
        profit target (delaying a close is safe) and is deliberately not used
        to bring a loss exit forward (firing early on an unverified assumption
        is not).
        """
        if not positions:
            return 0.0
        try:
            info = self.broker.get_symbol_info(self.symbol)
        except Exception:
            logger.warning("exit cost is not estimable: symbol info unavailable")
            return None
        if not info.pip_size:
            return None
        if info.profit_includes_exit_spread is True:
            # Already inside the broker's figure; charging it again would be a
            # second subtraction of the same money.
            return 0.0
        per_point = info.pip_value_per_lot
        half_spread_points = (info.spread / info.pip_size) / 2
        return round(sum(half_spread_points * p.volume * per_point for p in positions), 2)

    def _mark_positions(self, positions: list[Position]) -> None:
        """Remembers each open position's net mark, and notices which tickets
        have left the broker's open list without settling yet."""
        live = {(p.identifier or p.ticket) for p in positions}
        for p in positions:
            self._last_marks[p.identifier or p.ticket] = round(p.net_profit, 2)
        gone = set(self._last_marks) - live
        for ticket in gone:
            if ticket in self._known_tickets or ticket in self._awaiting_settlement:
                self._awaiting_settlement.add(ticket)

    def _pending_settlement_marked(self) -> float:
        """Last known mark of tickets that closed but have not settled.

        Without this the daily reading would spring back toward zero during the
        settlement gap: the loss would appear to vanish at precisely the moment
        it became permanent.
        """
        return round(sum(self._last_marks.get(t, 0.0) for t in self._awaiting_settlement), 2)

    def _day_risk(self, positions: list[Position] | None = None):
        """The day's risk picture, from reconciled sources only.

        Scoped to this account, symbol, magic and mode, so a manual order or
        another EA cannot move it. Deposits and withdrawals are account
        cashflows rather than trading results and are absent by construction.
        """
        if positions is None:
            try:
                positions = self.broker.get_open_positions(self.symbol, magic=self.magic_number)
            except Exception:
                positions = []
        totals = self._daily_totals or self._refresh_daily_totals()
        extra: list[str] = []
        if totals is None:
            extra.append("the day's settled trades could not be read")
        if self._persist_failed:
            extra.append(self._persist_failed)

        net, _gross, _known = self._basket_pnl(positions)
        return build_day_risk(
            trading_day=self._trading_day,
            settled_realized=totals.net if totals else 0.0,
            unsettled_count=totals.unsettled if totals else 0,
            open_positions_marked=net,
            day_open_marked=self._day_open_marked,
            pending_settlement_marked=self._pending_settlement_marked(),
            exit_reserve=self._estimated_exit_cost(positions),
            extra_incomplete=tuple(extra),
        )

    # ----------------------------------------------------- entry admission

    def verify_account(self, account) -> Admission:
        """Checks the BROKER's own account classification, not the app's label.

        The mode setting in the UI is a local string. It has never been checked
        against what the terminal is actually logged into, so a real account
        behind a "demo" label would have opened real positions with no extra
        confirmation. An unknown classification is refused, not assumed benign.
        Any identity change invalidates a previous confirmation.
        """
        identity = getattr(account, "account_id", None)
        trade_mode = getattr(account, "trade_mode", "unknown")
        # Kept so the dashboard and the pre-flight check can show what the
        # BROKER said, next to what the app is set to. They are different
        # facts and the owner has to be able to compare them.
        self._broker_trade_mode = trade_mode

        if not identity:
            return Admission.refuse("NO_TRADE: the broker did not report an account identity.", "account_id")
        if trade_mode not in ("demo", "real"):
            return Admission.refuse(
                f"NO_TRADE: the broker did not classify this account (trade_mode={trade_mode!r}). "
                "The app's own demo/real setting is not evidence of what the terminal is logged into.",
                "trade_mode",
            )
        if trade_mode != self.mode:
            return Admission.refuse(
                f"NO_TRADE: this app is set to {self.mode!r} but the broker reports a {trade_mode!r} "
                f"account ({identity}). Refusing rather than trusting the local label.",
            )
        if getattr(account, "trade_allowed", None) is False:
            return Admission.refuse(f"NO_TRADE: the broker reports trading is not allowed on {identity}.")
        self._account_verified = identity
        return Admission.ok()

    def _margin_admission(self, account, price, info) -> Admission:
        """Reserves margin for the WHOLE intended batch before any of it is sent.

        `calc_margin` answers for one operation only — it knows nothing about
        positions already open or orders already resting — so those are
        reconciled here instead of pretending one call covers the portfolio.
        An unknown figure blocks; it never becomes a permissive default.
        """
        free = getattr(account, "free_margin", None)
        if free is None:
            return Admission.refuse(
                "NO_TRADE: the broker did not report free margin, so the grid's margin cost cannot be "
                "checked. Refusing while that is unknown.",
                "free_margin",
            )

        buys, sells = self._grid_levels(price, info)
        per_order = []
        for side, levels in ((OrderSide.BUY, buys), (OrderSide.SELL, sells)):
            for level in levels:
                try:
                    needed = self.broker.calc_margin(self.symbol, side, self.lot_size, level)
                except Exception:
                    needed = None
                if needed is None:
                    return Admission.refuse(
                        "NO_TRADE: the broker could not calculate margin for a proposed order. "
                        "Refusing while the requirement is unknown.",
                        "calc_margin",
                    )
                per_order.append(needed)

        proposed = round(sum(per_order), 2)
        # Orders already resting are exposure this bot has requested but not
        # yet consumed margin for on fill; they are reserved alongside.
        resting = self._safe_pendings()
        if resting is None:
            return Admission.refuse(
                "NO_TRADE: resting orders could not be read, so existing reservations are unknown.",
                "pending_orders",
            )
        reserved_for_resting = 0.0
        for order in resting:
            try:
                side = OrderSide.BUY if order.order_type == PendingType.BUY_STOP else OrderSide.SELL
                value = self.broker.calc_margin(self.symbol, side, order.volume, order.price)
            except Exception:
                value = None
            if value is None:
                return Admission.refuse(
                    "NO_TRADE: margin for an already-resting order could not be calculated.",
                    "calc_margin",
                )
            reserved_for_resting += value

        required = round(proposed + reserved_for_resting, 2)
        if required > free:
            return Admission.refuse(
                f"NO_TRADE: the grid needs about ${required:.2f} of margin "
                f"(${proposed:.2f} proposed + ${reserved_for_resting:.2f} already reserved) "
                f"but only ${free:.2f} is free."
            )
        return Admission.ok()

    def _volume_admission(self, info) -> Admission:
        """The configured lot must actually be placeable at this broker.

        If what is affordable falls below the broker's minimum, that is a
        refusal — rounding upward would place more than was decided on.
        """
        lot = self.lot_size
        if not (lot and lot > 0 and lot == lot and lot not in (float("inf"), float("-inf"))):
            return Admission.refuse(f"NO_TRADE: the configured lot size ({lot!r}) is not a usable number.")
        if info.min_volume and lot + 1e-9 < info.min_volume:
            return Admission.refuse(
                f"NO_TRADE: the configured {lot} lot is below this broker's minimum of {info.min_volume}. "
                "Refusing rather than rounding the size up."
            )
        step = info.volume_step or 0.0
        if step > 0:
            steps = lot / step
            if abs(steps - round(steps)) > 1e-6:
                return Admission.refuse(
                    f"NO_TRADE: the configured {lot} lot is not a multiple of this broker's {step} step."
                )
        return Admission.ok()

    def _entry_gate(self, account) -> tuple[bool, str | None]:
        """Decides whether a NEW grid may be created at all.

        This runs before anything reaches the broker. Refusing here is the only
        protection that works on an account too small for the configured grid,
        because once the orders are resting the exposure already exists.

        Every check below refuses on an unknown. That is the difference between
        a guard and a comment: the previous free-margin check read a field that
        did not exist and therefore passed every single time.
        """
        self._entry_unknowns = ()

        # A halt outranks every other answer this gate can give. The live loop
        # also returns early while halted, but the gate is the function that
        # answers "may a new basket be created at all", and it returned True
        # during a halt — correct only because something else happened to
        # check first. Any other caller (a research profile, the admission
        # path, a UI preview) would have been told the wrong thing.
        if self._halt_reason:
            return False, f"HALTED: {self._halt_reason}"

        if self._persist_failed:
            return False, f"NO_TRADE: {self._persist_failed}"

        # A broker call stuck in the terminal means this process cannot
        # currently prove anything about the account. No second request is
        # fired to find out — that would risk two live orders for one decision
        # — so the honest response is to open nothing new until it returns.
        health = self._owner.snapshot_health()
        if health.blocked:
            return False, (
                f"BLOCKED: a broker call ({health.in_flight}) has been running for "
                f"{health.in_flight_ms / 1000:.1f}s and has not returned. No new exposure until it does. "
                "No competing request is sent while the original may still reach the broker."
            )
        if self._entries_paused:
            return False, f"PAUSED: {self._pause_reason or 'entries are paused'}"
        if self._close_intent and not self._close_intent.finished:
            return False, (
                f"CLOSING: a close is still in progress ({self._close_intent.state}). "
                "No new exposure until the broker confirms the old basket is gone."
            )

        if self.basket_stop_loss_usd <= 0 and self.max_daily_loss_usd <= 0:
            return False, (
                "RISK_CONFIG_REQUIRED: no basket stop loss and no daily loss limit are set, so nothing "
                "would end a losing basket. Set at least one before the bot may open exposure."
            )
        if self.capital_floor_usd <= 0:
            return False, (
                "RISK_CONFIG_REQUIRED: no capital floor is set. Set the balance below which no new grid "
                "may be placed before the bot may open exposure."
            )

        verified = self.verify_account(account)
        if not verified.allowed:
            self._entry_unknowns = verified.unknowns
            return False, verified.reason

        risk = self._day_risk()
        if not risk.complete:
            return False, (
                "NO_TRADE: today's risk accounting is incomplete — "
                + "; ".join(risk.incomplete_reasons)
                + ". Existing exposure is still protected."
            )

        affordable, reason = self._affordability(account, risk)
        if not affordable:
            return False, reason
        return True, None

    def _grid_levels(self, price: float, info) -> tuple[list[float], list[float]]:
        """The exact prices _build_grid would use. Shared so the affordability
        check measures the grid that would really be placed, not an idealised
        one."""
        return grid_math.grid_levels(
            price,
            grid_math.GridSpec(buy_levels=self.buy_stop_levels,
                               sell_levels=self.sell_stop_levels,
                               lot=self.lot_size, distance=self.grid_distance),
            grid_math.SymbolSpec.from_broker(info),
        )

    def _completed_grid_loss(self, price: float, info) -> float:
        """Marked loss of ONE named scenario, as a positive number in account currency.

        The scenario: every configured level fills at exactly its own price, the
        two sides end up with equal volume, and the basket is valued as if closed
        back at the reference price, having paid the entry spread once per fill.
        In that state the volumes cancel, the basket stops responding to price,
        and no later move recovers it.

        **This is an estimate of that scenario, not a maximum loss.** What it
        leaves out, all of which can make a real outcome worse:

        - the exit: no closing spread, no commission, no slippage reserve
        - commission and swap, which are not modelled here at all
        - partial or unequal fills, and one-directional exposure, which is not
          frozen and is bounded by the basket stop rather than by this figure
        - a cancellation race, a rejected close, or a gap during liquidation
        - `min_stop_distance` and `spread` moving after admission; both are read
          once, at admission, and both feed this number
        - conversion drift when the symbol is not quoted in account currency:
          `pip_value_per_lot` is the broker's snapshot at this moment

        Every input is named in `tools/grid_fit_report.py`, which prints the
        same arithmetic offline for stated symbol parameters. Nothing here is
        hardcoded per symbol or currency: `pip_size`, `pip_value_per_lot`,
        `spread` and `min_stop_distance` all come from the adapter, and
        `pip_value_per_lot` is documented as account currency per pip per lot.

        Used by admission as a declared policy: a grid whose estimate for this
        scenario does not fit a stated budget is refused. That is a policy about
        what may be opened, not a prediction that the scenario will happen.
        """
        return self._completed_grid_estimate(price, info).total

    def _completed_grid_estimate(self, price: float, info):
        """The same figure with its components, for the gate and the report."""
        return grid_math.completed_grid_estimate(
            price,
            grid_math.GridSpec(buy_levels=self.buy_stop_levels,
                               sell_levels=self.sell_stop_levels,
                               lot=self.lot_size, distance=self.grid_distance),
            grid_math.SymbolSpec.from_broker(info),
        )

    def _affordability(self, account, risk=None) -> tuple[bool, str | None]:
        try:
            info = self.broker.get_symbol_info(self.symbol)
            price = self.broker.get_current_price(self.symbol)
        except Exception as exc:
            return False, f"Cannot price the grid: {exc}. No orders placed."

        balance = account.balance or 0.0
        if balance <= 0:
            return False, "Account balance is zero or unreadable — no orders placed."

        # 0. The capital floor: a persistent line under the account, separate
        #    from the per-basket reserve below. One says "never trade this
        #    account down past here"; the other says "do not stake more than
        #    this share on the next basket". They are different questions.
        if balance <= self.capital_floor_usd:
            return False, (
                f"NO_TRADE: the ${balance:.2f} balance is at or below the ${self.capital_floor_usd:.2f} "
                "capital floor. No new grid is placed below it."
            )

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

        # 2b. The same comparison against the two other budgets the owner set.
        #     Check 2 asks whether the completed-grid estimate fits the basket
        #     budget; these ask whether it fits what is left of the day's budget
        #     and the headroom above the capital floor.
        #
        #     This is a DECLARED ADMISSION POLICY, not a prediction. A basket
        #     admitted with less headroom than the estimate may well reach its
        #     profit target and never approach the scenario at all. The policy
        #     says: do not open a basket whose named adverse scenario is larger
        #     than the budget that would have to absorb it. The reason for the
        #     refusal is the policy, not certainty about the outcome.
        #
        #     Boundaries, stated so nothing is counted twice:
        #     - `_consider_entry` only reaches admission with ZERO open
        #       positions, ZERO resting orders and ZERO unsettled closes for
        #       this account/symbol/magic, so `frozen` is the whole of the new
        #       exposure and `marked_result` carries none of it.
        #     - `marked_result` is the day's settled result plus the open-mark
        #       change plus anything awaiting settlement. It deliberately
        #       EXCLUDES the exit reserve (that is `risk_reading`), and `frozen`
        #       excludes exit costs too, so neither side of the comparison
        #       carries a closing-cost buffer. Both are entry-side figures.
        #     - units are account currency on both sides: the budgets are the
        #       owner's settings (named `_usd`, but whatever the account is
        #       denominated in) and `frozen` comes from the adapter's
        #       `pip_value_per_lot`, documented as account currency.
        if self.max_daily_loss_usd > 0:
            day = risk if risk is not None else self._day_risk()
            remaining = round(self.max_daily_loss_usd + day.marked_result, 2)
            if frozen > remaining:
                return False, (
                    f"NO_TRADE: the completed-grid estimate for this configuration is {frozen:.2f}, "
                    f"and {remaining:.2f} of today's {self.max_daily_loss_usd:.2f} loss budget is left "
                    f"(marked {day.marked_result:.2f}). Admission policy refuses a grid whose "
                    f"completed-grid estimate exceeds the budget that would have to absorb it. The "
                    f"estimate covers the fully filled equal-volume case and excludes exit costs, "
                    f"commission, swap and slippage."
                )

        floor_headroom = round(balance - self.capital_floor_usd, 2)
        if frozen > floor_headroom:
            return False, (
                f"NO_TRADE: the completed-grid estimate for this configuration is {frozen:.2f}, and "
                f"the {balance:.2f} balance has {floor_headroom:.2f} of headroom above the "
                f"{self.capital_floor_usd:.2f} capital floor. Admission policy refuses a grid whose "
                f"completed-grid estimate exceeds that headroom."
            )

        # 3. The broker's own volume rules.
        volume_ok = self._volume_admission(info)
        if not volume_ok.allowed:
            self._entry_unknowns = volume_ok.unknowns
            return False, volume_ok.reason

        # 4. Margin, from the broker's calculation, reserved for the whole
        #    intended batch plus whatever is already resting. This replaces a
        #    check that read a field the account model never had.
        margin_ok = self._margin_admission(account, price, info)
        if not margin_ok.allowed:
            self._entry_unknowns = margin_ok.unknowns
            return False, margin_ok.reason
        return True, None

    # ------------------------------------------------- durable risk state

    def _risk_key(self) -> str:
        """Keyed by the BROKER's account identity, not the app's mode label.

        `mode` used to be part of this key, so flipping the demo/real switch in
        the UI selected a different record and handed the account a fresh loss
        budget with an unresolved halt still outstanding. A cosmetic toggle must
        not be able to do that. Two genuinely different broker accounts still
        get separate state, because account_id is what distinguishes them.
        """
        return f"halt:{self._account_id}:{self.symbol}:{self.magic_number}"

    def _legacy_risk_keys(self) -> list[str]:
        """Keys written by earlier versions, which included the mode label."""
        return [f"halt:{self._account_id}:{self.symbol}:{self.magic_number}:{m}" for m in ("demo", "real")]

    def _restore_risk_state(self) -> None:
        """Reads back state written by an earlier run of this same identity.

        A read failure is NOT 'no halt'. It leaves the process unable to prove
        the account is unprotected, so it blocks new entries and says why.
        """
        saved: dict = {}
        try:
            saved = db_module.load_risk(self._risk_key()) or {}
            if not saved:
                # Migrate conservatively: an unresolved halt under an older,
                # mode-qualified key still counts. The strictest record wins.
                for legacy in self._legacy_risk_keys():
                    old = db_module.load_risk(legacy) or {}
                    if old.get("halt_reason") and not saved.get("halt_reason"):
                        saved = dict(old)
                    elif old and not saved:
                        saved = dict(old)
        except Exception as exc:
            logger.exception("could not read the persisted risk state")
            self._persist_failed = (
                f"risk state could not be read ({exc}). Treating protection as unproven: "
                "no new exposure until it can be read."
            )
            return

        reason = saved.get("halt_reason")
        if reason:
            self._halt_reason = reason
            logger.warning("restored an unresolved risk halt: %s", reason)

        peak = saved.get("equity_peak")
        if isinstance(peak, (int, float)) and peak > self._equity_peak:
            self._equity_peak = float(peak)

        intent = CloseIntent.from_dict(saved.get("close_intent"))
        if intent and not intent.finished:
            self._close_intent = intent
            self._basket_id = intent.basket_id
            logger.warning("restored an unfinished close intent: %s (%s)", intent.reason, intent.state)

        # The day identity is restored WITH its anchor. Restoring the anchor
        # only when the day already matched meant a fresh process — which has
        # no day yet — always came back with no anchor, and a reading with no
        # anchor is exactly what a restart must not produce. If the broker then
        # reports a different day, _roll_day replaces both.
        saved_day = saved.get("trading_day")
        if saved_day and (self._trading_day is None or self._trading_day == saved_day):
            self._trading_day = saved_day
            anchor = saved.get("day_open_marked")
            self._day_open_marked = float(anchor) if isinstance(anchor, (int, float)) else None

        marks = saved.get("last_marks")
        if isinstance(marks, dict):
            self._last_marks = {k: float(v) for k, v in marks.items() if isinstance(v, (int, float))}
        awaiting = saved.get("awaiting_settlement")
        if isinstance(awaiting, list):
            self._awaiting_settlement = {str(t) for t in awaiting}

        self._entries_paused = bool(saved.get("entries_paused", False))
        self._pause_reason = saved.get("pause_reason")

    def _persist_risk_state(self) -> None:
        """Writes the state protection depends on.

        A failed write is recorded and blocks new entries. Protection that was
        not written down survives only as long as this process does, and the
        owner is entitled to know that rather than be told it is durable.
        """
        payload = {
            "version": RISK_STATE_VERSION,
            "halt_reason": self._halt_reason,
            "equity_peak": self._equity_peak,
            "close_intent": self._close_intent.as_dict() if self._close_intent else None,
            "trading_day": self._trading_day,
            "day_open_marked": self._day_open_marked,
            # Bounded: only tickets that are open or still awaiting settlement.
            "last_marks": self._last_marks,
            "awaiting_settlement": sorted(self._awaiting_settlement),
            "entries_paused": self._entries_paused,
            "pause_reason": self._pause_reason,
        }
        try:
            db_module.save_risk(self._risk_key(), payload)
        except Exception as exc:
            logger.exception("could not persist the risk state")
            self._persist_failed = (
                f"risk state could not be written ({exc}). Protective actions continue, but a crash "
                "before this succeeds would lose them: no new exposure until the write succeeds."
            )
            return
        self._persist_failed = None

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
            previous_day = self._trading_day
            self._trading_day = day
            self._day = None
            # The opening anchor: what this bot was already holding as the day
            # turned. Subtracting it is what stops yesterday's unrealised loss
            # from being charged to today a second time. It is established for
            # EVERY binding, including the first, because a reading with no
            # anchor is incomplete and blocks new exposure.
            try:
                carried = self.broker.get_open_positions(self.symbol, magic=self.magic_number)
                self._day_open_marked = round(sum(p.net_profit for p in carried), 2)
            except Exception:
                logger.exception("could not mark the day's opening exposure")
                self._day_open_marked = None
            # Settlements belong to the day their broker timestamp names, so a
            # history response that arrives tomorrow does not drag yesterday's
            # close into tomorrow. Tickets still awaiting settlement keep their
            # marks across the boundary for the same reason.
            if not first_binding:
                logger.info(
                    "trading day rolled %s -> %s, opening exposure marked at %s",
                    previous_day, day, self._day_open_marked,
                )
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
            # The day identity and its anchor are written down immediately. A
            # restart later today has to find them, or the day's loss budget
            # would quietly start again from zero.
            self._persist_risk_state()
        self._refresh_daily_totals()
        # The peak is a high-water mark across the whole life of the account for
        # this bot, not per day. Resetting it every morning would let an account
        # bleed down indefinitely, one "fresh" day at a time.
        if equity > self._equity_peak:
            self._equity_peak = equity
            self._persist_risk_state()

    def _check_risk_limits(self, account, positions, pendings) -> bool:
        """The whole risk model. A grid has no per-trade stop, so if these do not
        fire nothing else will. Returns True when trading is halted.

        The daily limit is judged on the day's MARKED result - settled trades
        plus the change in open mark since the day's anchor plus anything still
        awaiting settlement. A floating loss counts against the limit the moment
        it exists, which is the whole point: the previous realised-only reading
        let a basket sit at -$200 without moving the number at all.
        """
        drawdown = 0.0
        if self._equity_peak > 0:
            drawdown = (self._equity_peak - account.equity) / self._equity_peak * 100

        risk = self._day_risk(positions)
        # The exit reserve is an UNVERIFIED estimate (see _estimated_exit_cost),
        # so it is not used to bring a loss exit forward. The marked result is.
        daily_reading = risk.marked_result

        reason = None
        cause = None
        if self.max_daily_loss_usd > 0 and daily_reading <= -self.max_daily_loss_usd:
            reason = (
                f"daily loss limit reached (marked {daily_reading:.2f} of {-self.max_daily_loss_usd:.2f}; "
                f"settled {risk.settled_realized:.2f}, open mark change {risk.open_mark_change:.2f})"
            )
            cause = CAUSE_DAILY_LOSS
        elif self.max_equity_drawdown_percent > 0 and drawdown >= self.max_equity_drawdown_percent:
            reason = f"equity drawdown {drawdown:.1f}% reached the {self.max_equity_drawdown_percent:.1f}% limit"
            cause = CAUSE_DRAWDOWN

        if reason is None:
            if self._halt_reason is None:
                return False
            # Already halted and no longer breaching. The halt still stands, and
            # any exposure it was meant to remove is still chased below.
            reason = self._halt_reason
            cause = CAUSE_DAILY_LOSS if reason.startswith("daily loss") else CAUSE_DRAWDOWN

        if self._halt_reason is None:
            self._halt_reason = reason
            # Written down BEFORE the liquidation is attempted. If the process
            # dies mid-close, the next run still knows it was halted.
            self._record_evidence(evidence_session.LIMIT_EVENT, cause=cause, reason=reason,
                                 day_risk=risk.as_dict())
            self._record_evidence(evidence_session.HALT, reason=reason, cause=cause)
            self._open_close_intent(cause, f"risk protection: {reason}")
            logger.warning("risk protection activated: %s — flattening and standing down", reason)
        elif self._close_intent is None and (positions or pendings):
            # Halt restored from storage with exposure still live: re-open the
            # intent so the closure is driven rather than merely reported.
            self._open_close_intent(cause, f"risk protection: {reason}")

        self._drive_close_intent(positions, pendings)
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

    def _settle_closed_trades(self, reason: str | None = None) -> None:
        """Marks tickets the broker no longer reports as open, using the broker's
        own realized figure so the dashboard matches the account history.

        `reason` is the engine's own explanation for the close. It is stamped
        only on rows that do not already carry one, so the first explanation —
        the one that actually ended the basket — is not overwritten by a later
        sweep that found the same ticket already gone.
        """
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
                    if reason and not record.close_reason:
                        record.close_reason = reason
                    # Stamped at settlement from the broker's candle, so the
                    # day a trade counts towards never shifts afterwards.
                    settled_at = self.broker.get_settlement_time(ticket) if hasattr(self.broker, "get_settlement_time") else None
                    record.close_time = settled_at or record.close_time
                    record.trading_day = self._trading_day_for(settled_at) if settled_at else self._trading_day
                    self._record_settlement(ticket, profit, reason or record.close_reason)
                self._known_tickets.discard(ticket)
            session.commit()
        self._refresh_daily_totals()

    def _record_settlement(self, ticket: str, profit: float | None, reason: str | None) -> None:
        """One settlement event per ticket, and a linked CORRECTION if it moves.

        The broker's realised figure is not always final at the moment a
        position leaves the open list - a swap or commission line can land
        after it. Rewriting the first event would destroy the only record of
        what was known when the decision was made, so a revision is appended
        and points at the row it revises.
        """
        try:
            prior = self._settlement_events.get(ticket)
            if prior is None:
                event = self.evidence.record(
                    evidence_session.SETTLEMENT, ticket=ticket, profit=profit,
                    profit_known=profit is not None, reason=reason,
                    basket_id=self._basket_id, trading_day=self._trading_day)
                if event is not None:
                    if len(self._settlement_events) >= 2000:
                        self._settlement_events.pop(next(iter(self._settlement_events)))
                    self._settlement_events[ticket] = (event["event_id"], profit)
                return
            event_id, recorded = prior
            if profit is not None and profit != recorded:
                self.evidence.correct(
                    event_id, reason="the broker revised the realised figure after settlement",
                    ticket=ticket, previous_profit=recorded, profit=profit)
                self._settlement_events[ticket] = (event_id, profit)
        except Exception:
            logger.exception("settlement evidence failed (continuing)")

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
                # The history sweep is where a revised realised figure usually
                # arrives. Routed through the same place so it links a
                # correction rather than quietly replacing what was recorded.
                self._record_settlement(item["ticket"], item.get("profit"),
                                        record.close_reason)
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
            # Everything below belonged to the PREVIOUS account. Carrying it
            # over would attach one account's halt, high-water mark and open
            # exposure to a different account. It is dropped first, then the
            # new identity's own record is read.
            self._halt_reason = None
            self._equity_peak = 0.0
            self._close_intent = None
            self._day_open_marked = None
            self._last_marks = {}
            self._awaiting_settlement = set()
            self._entries_paused = False
            self._pause_reason = None
            self._account_verified = None
            self._restore_risk_state()

    def _broadcast(
        self, account, positions, pendings, basket_profit: float, note: str | None = None,
        hedged: bool = False,
    ) -> None:
        if not self.on_update:
            return
        # None means the broker could not be read. It is NOT an empty list: a
        # failed read that rendered as zero positions was how live exposure
        # became invisible on the dashboard.
        positions_known = positions is not None
        pendings_known = pendings is not None
        positions = positions or []
        pendings = pendings or []
        buys = sum(1 for o in pendings if o.order_type == PendingType.BUY_STOP)
        sells = len(pendings) - buys
        payload = (
            {
                "type": "tick",
                **self._observation_header(),
                "positions_known": positions_known,
                "pending_orders_known": pendings_known,
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
        # Delivery is REPORTING. A consumer that raises — a dropped websocket,
        # a broken serializer — must not take the engine's cycle with it, or a
        # disconnected dashboard becomes a reason the stop never fires.
        try:
            self.on_update(payload)
        except Exception:
            self._broadcast_failures += 1
            logger.warning("snapshot delivery failed (%d so far); continuing",
                           self._broadcast_failures)

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
            "entry_unknowns": list(self._entry_unknowns),
            "entries_paused": self._entries_paused,
            "pause_reason": self._pause_reason,
            "close_intent": self._close_intent.as_dict() if self._close_intent else None,
            "persistence_error": self._persist_failed,
            "account_verified": self._account_verified,
            # What the broker itself classified the account as, or "unchecked"
            # before anything asked it. Never copied from the app's own mode.
            "broker_trade_mode": self._broker_trade_mode,
            "trading_window": self.session_label(),
            "in_session": self._within_session(),
            "capital_reserve_percent": self.capital_reserve_percent,
            "capital_floor_usd": self.capital_floor_usd,
            "basket_stop_loss_usd": self.basket_stop_loss_usd,
            "max_daily_loss_usd": self.max_daily_loss_usd,
            "max_equity_drawdown_percent": self.max_equity_drawdown_percent,
            # The accounting timezone the trading day is cut on. The dashboard
            # displays Pakistan time; this names what the DAY BOUNDARY uses,
            # which is a different statement and is not broker server time.
            "accounting_timezone": self.timezone_name,
            "broker_server_time_known": False,
            # --- execution health (Phase B) ---------------------------------
            "broker_owner": self._owner.snapshot_health().as_dict(),
            "protective_poll_seconds": self.protective_poll_seconds,
            # Phase B replaced this with the two cadences above. It is still
            # accepted so an existing settings file keeps loading, but it no
            # longer drives anything and the dashboard says so rather than
            # letting an owner change it and wonder why nothing happened.
            "poll_interval_seconds_retired": True,
            "poll_interval_seconds_value": self.poll_interval_seconds,
            "reporting_poll_seconds": self.reporting_poll_seconds,
            "protective_backoff_seconds": self._protective_backoff,
            "reporting_cycles_skipped": self._reporting_overruns,
            # Quote age here is the LOCAL age of the last observation, taken
            # from a monotonic clock. It is not a network latency figure: the
            # terminal's clock and this machine's are not synchronised, so
            # their difference is an unknown offset plus an unknown delay.
            "quote_local_age_ms": (
                round(self._last_quote.local_age_ms(), 1) if self._last_quote else None
            ),
            "quote_missing": bool(self._last_quote and self._last_quote.missing),
            "execution_timing": self._timing.report(),
            # The marked daily risk measure, alongside the realised cards below.
            # They are different quantities and are not expected to agree while
            # positions are open.
            "day_risk": self._day_risk().as_dict(),
            "daily_limit_remaining_usd": (
                round(self.max_daily_loss_usd + self._day_risk().marked_result, 2)
                if self.max_daily_loss_usd > 0 else None
            ),
            **self.daily_summary(),
            **self._observation_header(),
        }

    def _observation_header(self) -> dict:
        """Identity and ordering for anything that consumes a snapshot.

        A consumer uses `snapshot_seq` to discard an update that arrives after a
        newer one, and `account_id` to avoid merging observations of two
        different accounts into one screen.
        """
        self._snapshot_seq += 1
        return {
            "snapshot_seq": self._snapshot_seq,
            "state_version": RISK_STATE_VERSION,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "observation_account_id": self._account_id,
            "observation_symbol": self.symbol,
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
