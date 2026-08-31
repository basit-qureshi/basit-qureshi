import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import or_

from app.brokers import get_broker
from app.brokers.base import BrokerAdapter
from app.config import settings
from app.db import SessionLocal, TradeRecord, init_db
from app.engine.trading_engine import TradingEngine
from app.risk.risk_manager import RiskManager
from app.strategy.ema_rsi_strategy import EmaRsiStrategy
from app.strategy.momentum_scalp import MomentumScalpStrategy
from app.strategy.retest_rejection import RetestRejectionStrategy
from app.strategy.scalp_breakout import ScalpBreakoutStrategy
from app.strategy.smc_strategy import SmcStrategy

_SETTINGS_FILE = Path(__file__).resolve().parent.parent / "runtime_settings.json"

# Strategies the dashboard still offers. Anything else is retired: it exists in
# the code but can no longer be chosen, so a saved setting naming one has to be
# migrated rather than honoured. Silently running a strategy the user cannot see
# in the UI is worse than any wrong strategy — the dashboard says one thing
# while the account does another.
SELECTABLE_STRATEGIES = frozenset({"smc"})


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
            "fixed_lot_size": settings.fixed_lot_size,
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
            "strategy": settings.strategy,
            "sensitivity": settings.sensitivity,
            "max_trade_minutes": settings.max_trade_minutes,
            "quick_profit_usd": settings.quick_profit_usd,
            "max_spread_points": settings.max_spread_points,
            "trend_filter_timeframe": settings.trend_filter_timeframe,
            "basket_mode": settings.basket_mode,
            "basket_max_entries": settings.basket_max_entries,
            "basket_add_gap_points": settings.basket_add_gap_points,
            "basket_target_usd": settings.basket_target_usd,
            "basket_max_loss_usd": settings.basket_max_loss_usd,
            "smc_sl_buffer_points": settings.smc_sl_buffer_points,
            "smc_min_rr": settings.smc_min_rr,
            "smc_fallback_points": settings.smc_fallback_points,
            "smc_fallback_min_rr": settings.smc_fallback_min_rr,
            "smc_setup_expiry_minutes": settings.smc_setup_expiry_minutes,
            "smc_mss_max_age": settings.smc_mss_max_age,
            "smc_zone_tolerance_points": settings.smc_zone_tolerance_points,
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
            return
        self._migrate_retired_strategy()

    def _migrate_retired_strategy(self) -> None:
        """A settings file written before a strategy was retired still names it.

        Left alone, the dashboard shows the only strategy it offers while the
        engine quietly runs the old one — the trades then carry the old
        strategy's stop and target, which is very hard to diagnose from the
        outside. The saved value is therefore replaced with the configured one.
        """
        current = self.settings.get("strategy")
        if current in SELECTABLE_STRATEGIES:
            return
        replacement = settings.strategy if settings.strategy in SELECTABLE_STRATEGIES else "smc"
        self.settings["strategy"] = replacement
        self._save_persisted_settings()

    def _save_persisted_settings(self) -> None:
        try:
            _SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2))
        except Exception:
            pass

    def _build_engine(self) -> TradingEngine:
        s = self.settings
        if s["strategy"] == "smc":
            strategy = SmcStrategy.from_sensitivity(
                s["sensitivity"],
                sl_buffer_points=s["smc_sl_buffer_points"],
                min_rr=s["smc_min_rr"],
                fallback_points=s["smc_fallback_points"],
                fallback_min_rr=s["smc_fallback_min_rr"],
                mss_max_age=s["smc_mss_max_age"],
                zone_tolerance_points=s["smc_zone_tolerance_points"],
            )
        elif s["strategy"] == "momentum_scalp":
            strategy = MomentumScalpStrategy.from_sensitivity(
                s["sensitivity"], s["trend_filter_timeframe"]
            )
        elif s["strategy"] == "retest_rejection":
            strategy = RetestRejectionStrategy.from_sensitivity(s["sensitivity"])
        elif s["strategy"] == "scalp_breakout":
            strategy = ScalpBreakoutStrategy(ema_fast=s["ema_fast_period"], ema_slow=s["ema_slow_period"])
        else:
            strategy = EmaRsiStrategy(ema_fast=s["ema_fast_period"], ema_slow=s["ema_slow_period"])
        # In basket mode the group IS the trade, so the cap on concurrent
        # tickets has to be the basket size — otherwise max_open_trades would
        # stop the ladder halfway and leave a half-built basket unmanaged.
        max_open_trades = s["basket_max_entries"] if s["basket_mode"] else s["max_open_trades"]
        risk_manager = RiskManager(
            risk_percent=s["risk_percent"],
            fixed_lot_size=s["fixed_lot_size"],
            stop_loss_pips=s["stop_loss_pips"],
            take_profit_pips=s["take_profit_pips"],
            max_open_trades=max_open_trades,
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
            max_trade_minutes=s["max_trade_minutes"],
            quick_profit_usd=s["quick_profit_usd"],
            max_spread_points=s["max_spread_points"],
            basket_mode=s["basket_mode"],
            basket_max_entries=s["basket_max_entries"],
            basket_add_gap_points=s["basket_add_gap_points"],
            basket_target_usd=s["basket_target_usd"],
            basket_max_loss_usd=s["basket_max_loss_usd"],
            setup_expiry_minutes=s["smc_setup_expiry_minutes"],
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
        # A stale strategy can also arrive from the dashboard: a <select> whose
        # stored value is not among its options displays the first one while
        # still submitting the old value.
        self._migrate_retired_strategy()
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
                    try:
                        record.profit = self.broker.get_realized_profit(record.ticket)
                    except Exception:
                        pass
            # Also backfill already-CLOSED trades whose profit was never recorded
            # (None) or was recorded as 0.00 by the old balance-delta logic —
            # re-fetching from the broker's deal history returns the true value
            # either way (a genuinely break-even trade just gets 0 back again).
            missing = (
                session.query(TradeRecord)
                .filter(
                    TradeRecord.status == "CLOSED",
                    or_(TradeRecord.profit.is_(None), TradeRecord.profit == 0),
                )
                .all()
            )
            for record in missing:
                try:
                    profit = self.broker.get_realized_profit(record.ticket)
                except Exception:
                    continue
                if profit is not None:
                    record.profit = profit
            session.commit()


bot_manager = BotManager()
