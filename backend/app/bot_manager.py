import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import or_

from app.brokers import get_broker
from app.brokers.base import BrokerAdapter
from app.config import settings
from app import db as db_module
from app.db import TradeRecord, init_db
from app.engine.grid_engine import GridEngine
from app.engine.legacy_adapter import LegacyEngine
from app.engine import profiles as engine_profiles

_SETTINGS_FILE = Path(__file__).resolve().parent.parent / "runtime_settings.json"


class BotManager:
    """Owns the single broker connection + grid engine instance for the app,
    and lets settings be changed (while stopped) without restarting the process."""

    def __init__(self):
        self.broker: BrokerAdapter = get_broker()
        init_db()
        self.settings = {
            "symbol": settings.symbol,
            "timeframe": settings.timeframe,
            "strategy": "grid",
            "poll_interval_seconds": settings.poll_interval_seconds,
            "grid_lot_size": settings.grid_lot_size,
            "grid_buy_stop_levels": settings.grid_buy_stop_levels,
            "grid_sell_stop_levels": settings.grid_sell_stop_levels,
            "grid_distance": settings.grid_distance,
            "grid_basket_take_profit_usd": settings.grid_basket_take_profit_usd,
            "grid_daily_profit_target_usd": settings.grid_daily_profit_target_usd,
            "grid_basket_stop_loss_usd": settings.grid_basket_stop_loss_usd,
            "grid_max_open_positions": settings.grid_max_open_positions,
            "grid_max_daily_loss_usd": settings.grid_max_daily_loss_usd,
            "grid_max_equity_drawdown_percent": settings.grid_max_equity_drawdown_percent,
            "grid_capital_reserve_percent": settings.grid_capital_reserve_percent,
            "grid_capital_floor_usd": settings.grid_capital_floor_usd,
            "exit_commission_per_lot_usd": settings.exit_commission_per_lot_usd,
            "slippage_points_per_fill": settings.slippage_points_per_fill,
            "broker_profit_includes_exit_spread": settings.broker_profit_includes_exit_spread,
            "protective_poll_seconds": settings.protective_poll_seconds,
            "reporting_poll_seconds": settings.reporting_poll_seconds,
            "reporting_time_budget_ms": settings.reporting_time_budget_ms,
            "broker_stall_after_ms": settings.broker_stall_after_ms,
            "grid_magic_number": settings.grid_magic_number,
            "grid_trading_start_hour": settings.grid_trading_start_hour,
            "grid_trading_end_hour": settings.grid_trading_end_hour,
            "timezone": settings.timezone,
            "mode": settings.account_type,
            # Which engine build drives the account. The grid is the same in
            # both; see app/engine/profiles.py for what actually differs.
            "engine_profile": engine_profiles.normalise(settings.engine_profile),
        }
        self._load_persisted_settings()
        self._subscribers: list[asyncio.Queue] = []
        self.engine = self._build_engine()

    def _load_persisted_settings(self) -> None:
        """Settings changed from the dashboard are saved to runtime_settings.json
        so they survive a backend restart instead of silently reverting to
        whatever is in .env every time.

        Only keys the app still has are read back. A file written by an older
        version names settings that no longer exist, and honouring those is how
        a retired setting ends up quietly driving the bot.
        """
        saved = {}
        if _SETTINGS_FILE.exists():
            saved = json.loads(_SETTINGS_FILE.read_text())
        self.settings.update({k: v for k, v in saved.items() if k in self.settings})
        self.settings["strategy"] = "grid"
        # A settings file naming an engine this version does not have selects the
        # guarded one, never the legacy one. A stale or hand-edited file must not
        # be a way to end up running with fewer refusals than were chosen.
        self.settings["engine_profile"] = engine_profiles.normalise(self.settings.get("engine_profile"))
        if saved.get("_grid_restore_version") != 1:
            if _SETTINGS_FILE.exists():
                backup = _SETTINGS_FILE.with_name("runtime_settings.before_grid_restore.json")
                if not backup.exists():
                    backup.write_text(_SETTINGS_FILE.read_text())
            self.settings.update({
                "grid_lot_size": 0.01, "grid_buy_stop_levels": 10,
                "grid_sell_stop_levels": 10, "grid_distance": 0.30,
                "grid_basket_take_profit_usd": 10.0,
            })
            self._save_persisted_settings()

    def _save_persisted_settings(self) -> None:
        try:
            _SETTINGS_FILE.write_text(json.dumps({**self.settings, "_grid_restore_version": 1}, indent=2))
        except Exception:
            pass

    def _build_engine(self):
        """The engine named by `engine_profile`, built from the same settings.

        Both constructors take the same keyword arguments on purpose. The legacy
        engine accepts and discards the ones it predates, and reports them in
        `settings_not_applied`, so a setting that is doing nothing says so
        instead of appearing to hold.
        """
        s = self.settings
        build = LegacyEngine if engine_profiles.normalise(s.get("engine_profile")) == engine_profiles.LEGACY else GridEngine
        return build(
            broker=self.broker,
            symbol=s["symbol"],
            mode=s["mode"],
            lot_size=s["grid_lot_size"],
            buy_stop_levels=s["grid_buy_stop_levels"],
            sell_stop_levels=s["grid_sell_stop_levels"],
            grid_distance=s["grid_distance"],
            basket_take_profit_usd=s["grid_basket_take_profit_usd"],
            daily_profit_target_usd=s["grid_daily_profit_target_usd"],
            timezone_name=s["timezone"],
            basket_stop_loss_usd=s["grid_basket_stop_loss_usd"],
            max_open_positions=s["grid_max_open_positions"],
            max_daily_loss_usd=s["grid_max_daily_loss_usd"],
            max_equity_drawdown_percent=s["grid_max_equity_drawdown_percent"],
            capital_reserve_percent=s["grid_capital_reserve_percent"],
            capital_floor_usd=s["grid_capital_floor_usd"],
            exit_commission_per_lot=s["exit_commission_per_lot_usd"],
            slippage_points_per_fill=s["slippage_points_per_fill"],
            broker_profit_includes_exit_spread=s["broker_profit_includes_exit_spread"],
            protective_poll_seconds=s["protective_poll_seconds"],
            reporting_poll_seconds=s["reporting_poll_seconds"],
            reporting_time_budget_ms=s["reporting_time_budget_ms"],
            stall_after_ms=s["broker_stall_after_ms"],
            magic_number=s["grid_magic_number"],
            trading_start_hour=s["grid_trading_start_hour"],
            trading_end_hour=s["grid_trading_end_hour"],
            poll_interval_seconds=s["poll_interval_seconds"],
            on_update=self._on_update,
        )

    def _on_update(self, payload: dict) -> None:
        for q in list(self._subscribers):
            q.put_nowait(payload)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def update_settings(self, new_settings: dict) -> None:
        if self.engine.running:
            raise RuntimeError("Stop the bot before changing settings")
        self.settings.update(new_settings)
        self._save_persisted_settings()
        self.engine = self._build_engine()

    def set_mode(self, mode: str) -> None:
        if self.engine.running:
            raise RuntimeError("Stop the bot before switching mode")
        self.settings["mode"] = mode
        self._save_persisted_settings()
        self.engine = self._build_engine()

    def engine_profile(self) -> str:
        return engine_profiles.normalise(self.settings.get("engine_profile"))

    def set_engine_profile(self, profile: str) -> dict:
        """Swap which engine drives the account.

        Four refusals, and each one exists because switching through that state
        would lose something that must not be lost:

        * **Running.** A live loop owns a broker connection and an asyncio task.
          Replacing the object under it leaves the old task ticking an engine
          nothing can stop.
        * **Exposure open.** The two profiles manage a basket differently. A
          basket opened under one and inherited by the other would be managed by
          rules it was never admitted under, and the profile that placed it
          would no longer be the profile answering for it.
        * **An unresolved halt, in EITHER profile.** The two keep separate halt
          records so neither can clear the other's (see
          `legacy_adapter._risk_key`). That separation is only safe if switching
          cannot be used to step around a halt, so a halt anywhere blocks the
          switch until an owner clears it where it was raised.
        * **An unreadable broker.** Unknown is not flat.

        Returns what changed. Raises RuntimeError with the reason otherwise.
        """
        target = engine_profiles.normalise(profile)
        if not isinstance(profile, str) or profile.strip().lower() not in engine_profiles.ALL_PROFILES:
            raise ValueError(
                f"unknown engine profile {profile!r}; choose one of "
                f"{', '.join(sorted(engine_profiles.ALL_PROFILES))}"
            )
        current = self.engine_profile()
        if target == current:
            return {"changed": False, "profile": current,
                    "message": f"Already running the {engine_profiles.ALL_PROFILES[current].label}."}
        if self.engine.running:
            raise RuntimeError("Stop the bot before switching engine profile.")

        try:
            positions = self.engine.broker.get_open_positions(
                self.engine.symbol, magic=self.engine.magic_number)
            pendings = self.engine.broker.get_pending_orders(
                self.engine.symbol, magic=self.engine.magic_number)
        except Exception as exc:
            raise RuntimeError(
                f"Cannot confirm this bot is flat ({exc}), so the engine was not switched. "
                "An unreadable broker is not an empty one."
            ) from exc
        if positions or pendings:
            raise RuntimeError(
                f"Not switched: {len(positions)} position(s) and {len(pendings)} order(s) are still "
                "open. A basket opened under one engine must not be inherited by the other — close "
                "or let this basket finish first."
            )

        blocking = self._halt_blocking_switch(target)
        if blocking:
            raise RuntimeError(blocking)

        self.settings["engine_profile"] = target
        self._save_persisted_settings()
        self.engine = self._build_engine()
        return {
            "changed": True,
            "profile": target,
            "message": (
                f"Now running the {engine_profiles.ALL_PROFILES[target].label}. "
                "Risk settings, trade history and manual trades are untouched."
            ),
        }

    def _halt_blocking_switch(self, target: str) -> str | None:
        """An unresolved halt in either profile, named with where to clear it.

        The active engine is asked directly. The other profile's record is read
        from the database without building an engine, because building one is
        what the caller is trying to decide whether to do.
        """
        if getattr(self.engine, "_halt_reason", None):
            return (
                f"Not switched: this engine is halted — {self.engine._halt_reason} Clear the halt "
                "here first. Switching engines is not a way past a halt."
            )
        other = engine_profiles.GUARDED if target != engine_profiles.GUARDED else engine_profiles.LEGACY
        reason = self._stored_halt_reason(other)
        if reason:
            return (
                f"Not switched: the {engine_profiles.ALL_PROFILES[other].label} has an unresolved "
                f"halt — {reason} Switch to that profile, clear it there, then switch back."
            )
        return None

    def _stored_halt_reason(self, profile: str) -> str | None:
        engine = self.engine
        account_id = getattr(engine, "_account_id", "legacy")
        if profile == engine_profiles.LEGACY:
            keys = [f"legacyprofile:halt:{account_id}:{engine.symbol}:{engine.magic_number}:{m}"
                    for m in ("demo", "real")]
        else:
            keys = [f"halt:{account_id}:{engine.symbol}:{engine.magic_number}"]
            keys += [f"halt:{account_id}:{engine.symbol}:{engine.magic_number}:{m}"
                     for m in ("demo", "real")]
        for key in keys:
            try:
                saved = db_module.load_risk(key) or {}
            except Exception:
                # A record that cannot be read is not a record that says "no
                # halt". Refusing the switch is the conservative answer.
                return "its risk record could not be read, so whether it is halted is UNKNOWN."
            if saved.get("halt_reason"):
                return str(saved["halt_reason"])
        return None



bot_manager = BotManager()
