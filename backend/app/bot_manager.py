import asyncio
import json
from pathlib import Path

from app.brokers import get_broker
from app.brokers.base import BrokerAdapter
from app.config import settings
from app.db import init_db
from app.engine.grid_engine import GridEngine

_SETTINGS_FILE = Path(__file__).resolve().parent.parent / "runtime_settings.json"


class BotManager:
    """Owns the single broker connection + grid engine instance for the app,
    and lets settings be changed (while stopped) without restarting the process."""

    def __init__(self):
        self.broker: BrokerAdapter = get_broker()
        init_db()  # Never reconcile other accounts or legacy rows against this broker.
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
            "grid_magic_number": settings.grid_magic_number,
            "grid_trading_start_hour": settings.grid_trading_start_hour,
            "grid_trading_end_hour": settings.grid_trading_end_hour,
            "timezone": settings.timezone,
            "mode": settings.account_type,
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
        if not _SETTINGS_FILE.exists():
            return
        try:
            saved = json.loads(_SETTINGS_FILE.read_text())
        except Exception as exc:
            raise RuntimeError("Cannot read runtime_settings.json; restore or repair the saved settings") from exc
        self.settings.update({k: v for k, v in saved.items() if k in self.settings})
        self.settings["strategy"] = "grid"

    def _save_persisted_settings(self, candidate=None) -> None:
        temporary = _SETTINGS_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(candidate if candidate is not None else self.settings, indent=2))
        temporary.replace(_SETTINGS_FILE)

    def _build_engine(self) -> GridEngine:
        s = self.settings
        from app.api.schemas import SettingsUpdate
        SettingsUpdate(**{key: value for key, value in s.items() if key in SettingsUpdate.model_fields})
        engine = GridEngine(
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
            magic_number=s["grid_magic_number"],
            trading_start_hour=s["grid_trading_start_hour"],
            trading_end_hour=s["grid_trading_end_hour"],
            poll_interval_seconds=s["poll_interval_seconds"],
            on_update=self._on_update,
        )
        from app.ai.gate import AIGate
        engine.ai_gate = AIGate(settings.ai_model_path, settings.ai_mode, settings.ai_min_confidence)
        return engine

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
        if not self.broker.is_connected():
            self.broker.connect()
        if self.engine._own_state_exists():
            raise RuntimeError("Close existing bot positions and orders before changing settings")
        from app.api.schemas import SettingsUpdate
        SettingsUpdate(**new_settings)
        candidate = {**self.settings, **new_settings}
        if candidate["grid_buy_stop_levels"] + candidate["grid_sell_stop_levels"] > candidate["grid_max_open_positions"]:
            raise RuntimeError("Total grid levels exceed the position cap")
        self._save_persisted_settings(candidate)
        self.settings = candidate
        self.engine = self._build_engine()

    def set_mode(self, mode: str) -> None:
        if self.engine.running:
            raise RuntimeError("Stop the bot before switching mode")
        if not self.broker.is_connected():
            self.broker.connect()
        if self.engine._own_state_exists():
            raise RuntimeError("Close the existing basket before changing mode")
        if self.broker.get_account_info().trade_mode != mode:
            raise RuntimeError("Change the actual MT5 account first; this button cannot switch broker accounts")
        candidate = {**self.settings, "mode": mode}
        self._save_persisted_settings(candidate)
        self.settings = candidate
        self.engine = self._build_engine()



bot_manager = BotManager()
