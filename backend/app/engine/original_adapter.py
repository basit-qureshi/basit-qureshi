"""What makes the 2 September engine runnable inside today's application.

`OriginalGridEngine` is this repository's engine at `5aff68a`. The API and the
dashboard have moved on since then: they call methods that engine never had, and
read status keys it never produced. This subclass supplies exactly that surface
and nothing else.

The rule it follows. Every method here is reporting, plumbing, or an owner
control that did not exist in the original. **No method here overrides a
strategy decision**: not `_tick`, not `_build_grid`, not `_check_risk_limits`,
not `_check_daily_target`, not `_close_everything`, not `_gate_status`.
`tests/test_engine_profiles.py` asserts that by name, so this file cannot
quietly grow into a different strategy.

Four things that need saying plainly, because each one is a difference an owner
would otherwise discover the hard way:

1. **There is no entry gate at all.** No capital floor, no capital reserve, no
   completed-grid refusal, no closing-cost requirement, no symbol-valuation
   check. If nothing of this bot's is open and the next M1 candle has arrived, a
   grid is placed. That is the whole admission policy, and it is why this engine
   trades where the guarded one refuses.

2. **The basket target is judged on GROSS profit.** `sum(p.profit)` — price
   movement only. Swap and commission the broker has already booked are not in
   it, and nothing is reserved for the cost of closing. A basket closed at
   "+$10" pays out less than $10.

3. **The halt is not durable.** `_halt_reason` lives in memory and `start()`
   clears it. A restart, or pressing Start again, resumes trading after a daily
   loss or drawdown halt. That is the behaviour of the commit, not an oversight
   here, and `clear_halt` below does not pretend otherwise.

4. **Trades are recorded under the `legacy` account bucket**, because that is
   what the vendored engine writes — it stamps no account id, and the column
   defaults to `legacy`. `_account_id` is pinned to match, so the dashboard
   reads the same bucket the engine writes, and the trade history already in the
   database (which this engine produced) stays visible.
"""

from __future__ import annotations

import logging

from app.engine.lifecycle import Admission
from app.engine.original_engine import OriginalGridEngine
from app.evidence import session as evidence_session

logger = logging.getLogger("original_engine")

#: Named once so the API, the dashboard and the tests cannot disagree about it.
PROFILE_KEY = "original"

PROFILE_SUMMARY = (
    "The bot as it was on 2 September 2026. The grid is placed whenever nothing "
    "of this bot's is open and the next M1 candle has arrived — there is no "
    "entry gate of any kind in front of it."
)

#: Protection present in the guarded profile and ABSENT here. The dashboard
#: prints this list verbatim when the profile is active, so the choice is never
#: made from a one-word label.
PROFILE_MISSING = (
    "no entry gate at all: no capital floor, no capital reserve, no "
    "completed-grid refusal, no closing-cost requirement, no symbol-valuation "
    "check — a grid is placed whenever the book is empty and the candle turns",
    "the basket target is judged on GROSS profit: swap and commission are not "
    "in the number that triggers a close, and nothing is reserved for the exit",
    "the halt is not durable: it lives in memory and pressing Start clears it, "
    "so a restart resumes trading after a daily loss or drawdown halt",
    "no liquidation policy: exposure appearing after a halt is not cancelled "
    "and closed again",
    "no marked daily risk: the daily limit is judged on realised results and "
    "equity drawdown, never on floating loss",
    "no owner pause: Stop ends the management loop, and anything open at the "
    "broker stays open and unmanaged",
)


class _NoDayRisk:
    """Stands in for the marked daily-risk reading, which this engine has not got.

    It is not zero, and it is not an error. Returning zero would put a confident
    number on screen for a measure this engine never computes.
    """

    def as_dict(self) -> dict:
        return {
            "available": False,
            "reason": (
                "the 2 September engine does not compute a marked daily risk reading: "
                "its daily limit is judged on realised results and equity drawdown"
            ),
            "settled_realized": None,
            "open_marked": None,
            "pending_settlement_marked": None,
            "marked_result": None,
            "exit_reserve": None,
            "risk_reading": None,
        }


class OriginalEngine(OriginalGridEngine):
    """The 2 September engine, wearing today's reporting surface."""

    profile = PROFILE_KEY

    #: See point 4 in the module docstring.
    _account_id = "legacy"

    def __init__(self, *args, **kwargs):
        # Accepted and discarded, by name, so a caller that builds either engine
        # from one settings dictionary does not have to know which keys belong
        # to which. Dropping them silently would be worse: `status()` reports
        # every one, so a setting that is doing nothing says so rather than
        # appearing to hold.
        self.ignored_settings = {
            key: kwargs.pop(key)
            for key in (
                "capital_floor_usd",
                "capital_reserve_percent",
                "exit_commission_per_lot",
                "slippage_points_per_fill",
                "broker_profit_includes_exit_spread",
                "protective_poll_seconds",
                "reporting_poll_seconds",
                "reporting_time_budget_ms",
                "stall_after_ms",
            )
            if key in kwargs
        }
        self._snapshot_seq = 0
        self._broker_trade_mode = "unchecked"
        self._account_verified = None
        self.evidence = evidence_session.NullEvidence()
        super().__init__(*args, **kwargs)

    # ------------------------------------------------- owner control surface

    def stop(self) -> tuple[bool, str]:
        """The original `stop()` returned nothing. The endpoint reports what is
        left open, so a cancelled loop is never presented as a flat account."""
        super().stop()
        try:
            positions = self.broker.get_open_positions(self.symbol, magic=self.magic_number)
            pendings = self.broker.get_pending_orders(self.symbol, magic=self.magic_number)
        except Exception as exc:
            return False, (
                f"Management stopped, but the broker could not be read ({exc}) — whether anything "
                "is still open is UNKNOWN. Check the terminal."
            )
        if positions or pendings:
            return False, (
                f"Management stopped. {len(positions)} position(s) and {len(pendings)} order(s) are "
                "STILL OPEN at the broker and are no longer being monitored by this bot. Nothing "
                "will close them. This engine has no pause that keeps protecting."
            )
        return True, "Management stopped. The broker reports no positions or orders for this bot."

    _NO_PAUSE = (
        "The 2 September engine has no owner pause: it is the bot as it was before one existed. "
        "Stop ends the management loop, and anything open at the broker stays open and unmanaged. "
        "Switch to the guarded engine for pause, resume and close-and-pause."
    )

    def pause_entries(self) -> tuple[bool, str]:
        return False, self._NO_PAUSE

    def resume_entries(self) -> tuple[bool, str]:
        return False, self._NO_PAUSE

    def close_and_pause(self, reason: str = "closed by owner") -> tuple[bool, str]:
        """Flatten this bot's own exposure. There is no pause to hold afterwards.

        Why this is not simply refused like the other two. Closing on demand is
        an owner's instruction to the broker, not a decision the strategy makes,
        and leaving an owner unable to flatten from the dashboard would be a
        safety regression introduced by offering this profile at all.

        Why the loop is stopped first. This engine has no entries-paused latch,
        so a running loop would rebuild a grid on the very next candle and the
        button would be a lie. Stopping first makes the instruction hold; it
        adds no protection, because a cancelled loop decides nothing. Only this
        bot's magic number is touched, so a manual trade is never closed here.
        """
        super().stop()
        try:
            positions = self.broker.get_open_positions(self.symbol, magic=self.magic_number)
            pendings = self.broker.get_pending_orders(self.symbol, magic=self.magic_number)
        except Exception as exc:
            return False, (
                f"Management stopped, but the broker could not be read ({exc}), so nothing was "
                "closed and what is open is UNKNOWN. Check the terminal."
            )
        if not positions and not pendings:
            return True, "Management stopped. The broker already reports no positions or orders for this bot."

        self._close_everything(positions, pendings, reason)
        try:
            left_positions = self.broker.get_open_positions(self.symbol, magic=self.magic_number)
            left_pendings = self.broker.get_pending_orders(self.symbol, magic=self.magic_number)
        except Exception as exc:
            return False, f"Close attempted, but the result could not be confirmed ({exc}). Check the terminal."
        if left_positions or left_pendings:
            return False, (
                f"Closed what it could. {len(left_positions)} position(s) and {len(left_pendings)} "
                "order(s) are STILL open and the loop is stopped, so nothing will retry. Close them "
                "in the terminal."
            )
        return True, (
            "Closed, and the management loop is stopped. This engine has no pause that keeps entries "
            "shut while the loop runs, so Start will build a fresh grid on the next candle."
        )

    def clear_halt(self) -> tuple[bool, str]:
        """Release the in-memory halt, and say that it was only ever in memory.

        The guarded engine refuses this while exposure is open, because its halt
        is durable and clearing one over live positions turns a breach into a
        larger one. Here there is nothing durable to refuse on: this engine's
        own `start()` clears the halt, so refusing the button while Start does
        it silently would be theatre. The message names that instead.
        """
        had = self._halt_reason
        self._halt_reason = None
        if not had:
            return True, "No halt was set."
        return True, (
            f"Halt cleared ({had}). Note: this engine keeps no durable halt — pressing Start "
            "clears it too, and a restart does not restore it."
        )

    def verify_account(self, account) -> Admission:
        """The broker's own account classification, checked the same way in both
        profiles.

        This is not strategy, and it is not one of the protections this profile
        is defined by the absence of: it is the check that stops a REAL account
        behind a "demo" label from being traded at all. Weakening it to make a
        comparison 'fair' would make the comparison dangerous instead.
        """
        identity = getattr(account, "account_id", None)
        trade_mode = getattr(account, "trade_mode", "unknown")
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
                f"account ({identity}). Refusing rather than trusting the local label."
            )
        if getattr(account, "trade_allowed", None) is False:
            return Admission.refuse(f"NO_TRADE: the broker reports trading is not allowed on {identity}.")
        self._account_verified = identity
        return Admission.ok()

    # ------------------------------------------------------------- reporting

    def _basket_pnl_display(self, positions) -> tuple[float, float, bool]:
        """(net, gross, costs known) — reported as this engine judges it.

        It judges the basket on GROSS position profit, so the figure shown as
        the basket's value is that same gross number. `costs_known` is False
        every time, because no cost is inside it: reporting a net the engine
        does not act on would put a number on screen that the close is not
        triggered by.
        """
        gross = round(sum(p.profit or 0.0 for p in positions), 2)
        return gross, gross, False

    def _estimated_exit_cost(self, positions) -> float | None:
        """None: this engine estimates no exit cost and reserves nothing for it."""
        return None

    def _day_risk(self, positions=None) -> _NoDayRisk:
        return _NoDayRisk()

    def _observation_header(self) -> dict:
        from datetime import datetime, timezone

        self._snapshot_seq += 1
        return {
            "snapshot_seq": self._snapshot_seq,
            "state_version": 0,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "observation_account_id": self._account_id,
            "observation_symbol": self.symbol,
        }

    def session_label(self) -> str:
        """How the trading window reads to the owner, in the configured clock.

        The 2 September engine has `_within_session` but never named the window
        for the dashboard. Note what it does NOT have: that check reads the
        machine's UTC hour directly, where today's engine converts into the
        configured timezone first. The label says UTC here because that is what
        this engine actually compares against — printing "Karachi" over a UTC
        comparison would be a five-hour lie.
        """
        if self.trading_start_hour == 0 and self.trading_end_hour >= 24:
            return "always on"
        return f"{self.trading_start_hour:02d}:00-{self.trading_end_hour:02d}:00 UTC"

    def _profile_report(self) -> dict:
        return {
            "engine_profile": PROFILE_KEY,
            "engine_profile_summary": PROFILE_SUMMARY,
            "engine_profile_missing": list(PROFILE_MISSING),
            "engine_profile_source_commit": "5aff68a",
            "settings_not_applied": sorted(self.ignored_settings),
        }

    def status(self) -> dict:
        """The original status, plus the keys today's dashboard reads.

        Every added key is either the profile's identity or an honest `False` /
        `None` for a mechanism this engine does not have. None of them is a
        placeholder number.
        """
        return {
            **super().status(),
            **self._profile_report(),
            "strategy_name": "OriginalGridEngine",
            "account_id": self._account_id,
            "capital_floor_usd": None,
            "capital_reserve_percent": None,
            "entry_blocked": False,
            "entry_block_reason": None,
            "entry_unknowns": [],
            "entries_paused": False,
            "pause_reason": None,
            "close_intent": None,
            "persistence_error": None,
            "account_verified": self._account_verified,
            "broker_trade_mode": self._broker_trade_mode,
            "accounting_timezone": self.timezone_name,
            "broker_server_time_known": False,
            "trading_window": self.session_label(),
            "in_session": self._within_session(),
            "broker_owner": None,
            "protective_poll_seconds": None,
            "poll_interval_seconds_retired": False,
            "poll_interval_seconds_value": self.poll_interval_seconds,
            "reporting_poll_seconds": None,
            "protective_backoff_seconds": 0,
            "reporting_cycles_skipped": 0,
            "quote_local_age_ms": None,
            "quote_missing": False,
            "stop_distance": None,
            "completed_grid_estimate": None,
            "execution_timing": None,
            "scheduling": None,
            "liquidation_policy": None,
            "closing_costs": None,
            "day_risk": self._day_risk().as_dict(),
            "daily_limit_remaining_usd": None,
            "basket_stop_loss_usd": self.basket_stop_loss_usd,
            "max_daily_loss_usd": self.max_daily_loss_usd,
            "max_equity_drawdown_percent": self.max_equity_drawdown_percent,
            **self._observation_header(),
        }

    def _broadcast(self, account, positions, pendings, basket_profit: float,
                   note: str | None = None, hedged: bool = False) -> None:
        """The original broadcast, with the snapshot header today's client needs
        to order updates and to notice an account change."""
        if not self.on_update:
            return
        captured: dict = {}
        original = self.on_update
        try:
            self.on_update = captured.update
            super()._broadcast(account, positions, pendings, basket_profit, note=note, hedged=hedged)
        finally:
            self.on_update = original
        if not captured:
            return
        captured.setdefault("positions_known", True)
        captured["engine_profile"] = PROFILE_KEY
        captured.update(self._observation_header())
        original(captured)
