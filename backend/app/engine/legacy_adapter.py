"""What makes the original engine runnable inside today's application.

`LegacyGridEngine` is the file this repository had at `1116af1`. The API and the
dashboard have moved on since then: they call methods that engine never had, and
read status keys it never produced. This subclass supplies exactly that surface
and nothing else.

The rule it follows. Every method here is reporting, plumbing, or an owner
control that did not exist in the original. **No method here overrides a
strategy decision**: not `_tick`, not `_build_grid`, not `_grid_levels`, not
`_entry_gate`, not `_affordability`, not `_check_risk_limits`, not
`_completed_grid_loss`. `tests/test_engine_profiles.py` asserts that, by name,
so this file cannot quietly grow into a third strategy.

Two deliberate differences from the original, both outside the strategy:

1. **Its own risk record.** The original keyed persisted state as
   `halt:<account>:<symbol>:<magic>:<mode>` — which today's engine reads as one
   of its own *legacy* keys and migrates from. Leaving them shared would let
   whichever profile ran last overwrite the other's halt. `_risk_key` here moves
   the record under a `legacyprofile:` prefix, so a halt raised in one profile
   survives in that profile and is never silently cleared by the other.
   `BotManager.set_engine_profile` refuses to switch while either has an
   unresolved halt, so the separation cannot be used to escape one.

2. **Owner controls answer honestly instead of pretending.** The original had no
   pause, no resume and no close-and-pause: Stop was the only button, and Stop
   ends the management loop while leaving positions live at the broker. Those
   endpoints therefore refuse here with that explanation rather than being
   emulated, because emulating them would be adding protection the profile is
   supposed to be without.
"""

from __future__ import annotations

import logging

from app.engine.legacy_engine import LegacyGridEngine
from app.engine.lifecycle import Admission
from app.evidence import session as evidence_session

logger = logging.getLogger("legacy_engine")

#: Named once so the API, the dashboard and the tests cannot disagree about it.
PROFILE_KEY = "legacy"

#: What an owner is actually choosing. Written here rather than in the UI so the
#: screen and the API serve the same sentence.
PROFILE_SUMMARY = (
    "The engine as it was at commit 1116af1, before the capital floor, the "
    "closing-cost contract, the symbol-valuation refusal, the liquidation "
    "policy, the marked daily-risk reading and the owner pause existed. The "
    "grid itself is identical; what differs is how much has to be known before "
    "it may be placed."
)

#: Protection present in the guarded profile and ABSENT here. The dashboard
#: prints this list verbatim when the profile is active, so the choice is never
#: made from a one-word label.
PROFILE_MISSING = (
    "no capital floor: nothing refuses a grid because the balance is near a "
    "line the account must not be traded past",
    "no closing-cost contract: the exit is estimated as half the current "
    "spread, with commission, swap and slippage left out rather than blocking",
    "no symbol-valuation refusal: an unreadable tick value or point size "
    "raises an error on the tick instead of refusing entry",
    "no liquidation policy: exposure appearing after a risk halt is not "
    "cancelled and closed again",
    "no marked daily risk: the daily limit is judged on realised results and "
    "equity drawdown, not on a reading that includes floating loss",
    "no owner pause: Stop ends the management loop, and anything open at the "
    "broker stays open and unmanaged",
)


class _NoDayRisk:
    """Stands in for the marked daily-risk reading, which this profile has not got.

    It is not zero, and it is not an error. Returning zero here would put a
    confident number on a screen for a measure this engine never computes.
    """

    def as_dict(self) -> dict:
        return {
            "available": False,
            "reason": (
                "the legacy profile does not compute a marked daily risk reading: "
                "its daily limit is judged on realised results and equity drawdown"
            ),
            "settled_realized": None,
            "open_marked": None,
            "pending_settlement_marked": None,
            "marked_result": None,
            "exit_reserve": None,
            "risk_reading": None,
        }


class LegacyEngine(LegacyGridEngine):
    """The original engine, wearing today's reporting and control surface."""

    profile = PROFILE_KEY

    def __init__(self, *args, **kwargs):
        # Accepted and discarded, by name, so a caller that builds either engine
        # from one settings dictionary does not have to know which keys belong
        # to which. Dropping them silently would be worse: `status()` reports
        # every one so the owner can see what their setting is not doing.
        self.ignored_settings = {
            key: kwargs.pop(key)
            for key in (
                "capital_floor_usd",
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
        self.evidence = evidence_session.NullEvidence()
        super().__init__(*args, **kwargs)

    # ------------------------------------------------------- state isolation

    def _risk_key(self) -> str:
        """This profile's own halt record. See the module docstring, point 1."""
        return f"legacyprofile:halt:{self._account_id}:{self.symbol}:{self.magic_number}:{self.mode}"

    # ------------------------------------------------- owner control surface

    def stop(self) -> tuple[bool, str]:
        """The original `stop()` returned nothing. The endpoint reports what is
        left open, so the caller cannot present a cancelled loop as a flat
        account — which is the one thing the original Stop button got wrong."""
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
                "will close them. The legacy profile has no pause that keeps protecting."
            )
        return True, "Management stopped. The broker reports no positions or orders for this bot."

    _NO_PAUSE = (
        "The legacy profile has no owner pause: it is the engine as it was before one existed. "
        "Stop ends the management loop, and anything open at the broker stays open and unmanaged. "
        "Switch to the guarded profile for pause, resume and close-and-pause."
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
        safety regression introduced by offering this profile at all — the
        original lacked the button only because it had not been built yet.

        Why the loop is stopped first. This engine has no entries-paused latch,
        so a running loop would rebuild a grid on the very next candle and the
        button would be a lie. Stopping first makes the instruction hold; it
        adds no protection, because a cancelled loop is not deciding anything.
        Only this bot's magic number is touched, so a manual trade is never
        closed by this.
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
            "Closed, and the management loop is stopped. The original engine has no pause that keeps "
            "entries shut while the loop runs, so Start will build a fresh grid on the next candle."
        )

    def verify_account(self, account) -> Admission:
        """The broker's own account classification, checked the same way in both
        profiles.

        This is not strategy and it is not one of the protections the legacy
        profile is defined by the absence of: it is the check that stops a REAL
        account behind a "demo" label from being traded at all. Weakening it to
        make a comparison 'fair' would make the comparison dangerous instead.
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
        """The original `_basket_pnl` already rounds. Named for today's callers."""
        return self._basket_pnl(positions)

    def _day_risk(self, positions=None) -> _NoDayRisk:
        return _NoDayRisk()

    def _observation_header(self) -> dict:
        from datetime import datetime, timezone

        self._snapshot_seq += 1
        return {
            "snapshot_seq": self._snapshot_seq,
            "state_version": 1,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "observation_account_id": self._account_id,
            "observation_symbol": self.symbol,
        }

    def _profile_report(self) -> dict:
        return {
            "engine_profile": PROFILE_KEY,
            "engine_profile_summary": PROFILE_SUMMARY,
            "engine_profile_missing": list(PROFILE_MISSING),
            "engine_profile_source_commit": "1116af1",
            # Settings the dashboard still shows, which this profile does not
            # read. Saying so beats letting an owner set a capital floor here
            # and believe it is holding.
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
            "strategy_name": "LegacyGridEngine",
            "capital_floor_usd": None,
            "entry_unknowns": [],
            "entries_paused": False,
            "pause_reason": None,
            "close_intent": None,
            "persistence_error": None,
            "account_verified": getattr(self, "_account_verified", None),
            "broker_trade_mode": getattr(self, "_broker_trade_mode", "unchecked"),
            "accounting_timezone": self.timezone_name,
            "broker_server_time_known": False,
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
            **self._observation_header(),
        }

    def _broadcast(self, account, positions, pendings, basket_profit: float,
                   note: str | None = None, hedged: bool = False) -> None:
        """The original broadcast, with the snapshot header today's client needs
        to order updates and to notice an account change."""
        if not self.on_update:
            return
        captured = {}
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
