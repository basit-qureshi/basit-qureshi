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
        saved = {}
        if _SETTINGS_FILE.exists():
            saved = json.loads(_SETTINGS_FILE.read_text())
        self.settings.update({k: v for k, v in saved.items() if k in self.settings})
        self.settings["strategy"] = "grid"
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

    def _build_engine(self) -> GridEngine:
        s = self.settings
        return GridEngine(
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



bot_manager = BotManager()
