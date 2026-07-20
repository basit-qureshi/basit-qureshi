import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from app.brokers import get_broker
from app.brokers.base import BrokerAdapter
from app.config import settings
from app.db import SessionLocal, TradeRecord, init_db
from app.engine.trading_engine import TradingEngine
from app.risk.risk_manager import RiskManager
from app.strategy.ema_rsi_strategy import EmaRsiStrategy

_SETTINGS_FILE = Path(__file__).resolve().parent.parent / "runtime_settings.json"


class BotManager:
    """Owns the single broker connection + trading engine instance for the app,
    and lets settings be changed (while stopped) without restarting the process."""

    def __init__(self):
        self.broker: BrokerAdapter = get_broker()
        self._reconcile_stale_open_trades()
        self.settings = {
            "symbol": settings.symbol,
            "timeframe": settings.timeframe,
            "risk_percent": settings.risk_percent,
            "stop_loss_pips": settings.stop_loss_pips,
            "take_profit_pips": settings.take_profit_pips,
            "max_open_trades": settings.max_open_trades,
            "max_daily_loss_percent": settings.max_daily_loss_percent,
            "max_daily_trades": settings.max_daily_trades,
            "breakeven_trigger_pips": settings.breakeven_trigger_pips,
            "trailing_stop_pips": settings.trailing_stop_pips,
            "poll_interval_seconds": settings.poll_interval_seconds,
            "ema_fast_period": settings.ema_fast_period,
            "ema_slow_period": settings.ema_slow_period,
            "mode": settings.account_type,
        }
        self._load_persisted_settings()
        self._subscribers: list[asyncio.Queue] = []
        self.engine = self._build_engine()

    def _load_persisted_settings(self) -> None:
        """Settings changed from the dashboard are saved to runtime_settings.json
        so they survive a backend restart instead of silently reverting to
        whatever is in .env every time."""
        if not _SETTINGS_FILE.exists():
            return
        try:
            saved = json.loads(_SETTINGS_FILE.read_text())
            self.settings.update({k: v for k, v in saved.items() if k in self.settings})
        except Exception:
            pass

    def _save_persisted_settings(self) -> None:
        try:
            _SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2))
        except Exception:
            pass

    def _build_engine(self) -> TradingEngine:
        s = self.settings
        strategy = EmaRsiStrategy(ema_fast=s["ema_fast_period"], ema_slow=s["ema_slow_period"])
        risk_manager = RiskManager(
            risk_percent=s["risk_percent"],
            stop_loss_pips=s["stop_loss_pips"],
            take_profit_pips=s["take_profit_pips"],
            max_open_trades=s["max_open_trades"],
            max_daily_loss_percent=s["max_daily_loss_percent"],
            max_daily_trades=s["max_daily_trades"],
            breakeven_trigger_pips=s["breakeven_trigger_pips"],
            trailing_stop_pips=s["trailing_stop_pips"],
        )
        return TradingEngine(
            broker=self.broker,
            strategy=strategy,
            risk_manager=risk_manager,
            symbol=s["symbol"],
            timeframe=s["timeframe"],
            poll_interval_seconds=s["poll_interval_seconds"],
            mode=s["mode"],
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

    def _reconcile_stale_open_trades(self) -> None:
        """On startup, any DB trade still marked OPEN that the broker no longer
        reports as open (app restarted while the mock broker's in-memory state
        was lost, or a real position got closed while the bot was offline) is
        marked CLOSED so it doesn't linger in the stats forever. The realized
        profit for these is unknown, so it's left blank rather than guessed.
        """
        init_db()
        try:
            if not self.broker.is_connected():
                self.broker.connect()
            live_tickets = {p.ticket for p in self.broker.get_open_positions()}
        except Exception:
            return
        with SessionLocal() as session:
            stale = session.query(TradeRecord).filter(TradeRecord.status == "OPEN").all()
            for record in stale:
                if record.ticket not in live_tickets:
                    record.status = "CLOSED"
                    record.close_time = datetime.now(timezone.utc)
            session.commit()


bot_manager = BotManager()
