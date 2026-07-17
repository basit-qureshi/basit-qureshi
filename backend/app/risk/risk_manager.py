from dataclasses import dataclass
from datetime import date

from app.brokers.base import SymbolInfo


@dataclass
class RiskDecision:
    allowed: bool
    reason: str
    volume: float = 0.0


class RiskManager:
    """Every rule that keeps the bot from blowing up the account lives here,
    separate from the strategy, so a strategy signal can never bypass it.
    """

    def __init__(
        self,
        risk_percent: float,
        stop_loss_pips: float,
        take_profit_pips: float,
        max_open_trades: int,
        max_daily_loss_percent: float,
        max_daily_trades: int = 0,
        breakeven_trigger_pips: float = 0,
        trailing_stop_pips: float = 0,
    ):
        self.risk_percent = risk_percent
        self.stop_loss_pips = stop_loss_pips
        self.take_profit_pips = take_profit_pips
        self.max_open_trades = max_open_trades
        self.max_daily_loss_percent = max_daily_loss_percent
        self.max_daily_trades = max_daily_trades  # 0 = unlimited
        self.breakeven_trigger_pips = breakeven_trigger_pips  # 0 = disabled
        self.trailing_stop_pips = trailing_stop_pips  # 0 = no trailing beyond breakeven

        self._day: date | None = None
        self._day_start_equity: float = 0.0
        self._day_trade_count: int = 0

    def _roll_day_if_needed(self, equity: float) -> None:
        today = date.today()
        if self._day != today:
            self._day = today
            self._day_start_equity = equity
            self._day_trade_count = 0

    def record_trade_opened(self) -> None:
        self._day_trade_count += 1

    def daily_loss_percent(self, equity: float) -> float:
        self._roll_day_if_needed(equity)
        if self._day_start_equity <= 0:
            return 0.0
        return max(0.0, (self._day_start_equity - equity) / self._day_start_equity * 100)

    def check_daily_loss_limit(self, equity: float) -> bool:
        """Returns True if trading should be halted for the rest of the day."""
        return self.daily_loss_percent(equity) >= self.max_daily_loss_percent

    def calculate_position_size(self, balance: float, symbol_info: SymbolInfo) -> float:
        risk_amount = balance * (self.risk_percent / 100)
        pip_value_per_lot = symbol_info.pip_value_per_lot or 10.0
        raw_lots = risk_amount / (self.stop_loss_pips * pip_value_per_lot)
        step = symbol_info.volume_step or 0.01
        lots = max(symbol_info.min_volume, round(raw_lots / step) * step)
        return round(lots, 2)

    def evaluate(self, balance: float, equity: float, open_trade_count: int, symbol_info: SymbolInfo) -> RiskDecision:
        self._roll_day_if_needed(equity)

        if self.check_daily_loss_limit(equity):
            return RiskDecision(False, f"Daily loss limit reached ({self.max_daily_loss_percent}% of starting equity)")

        if self.max_daily_trades and self._day_trade_count >= self.max_daily_trades:
            return RiskDecision(False, f"Max daily trades reached ({self.max_daily_trades})")

        if open_trade_count >= self.max_open_trades:
            return RiskDecision(False, f"Max open trades reached ({self.max_open_trades})")

        volume = self.calculate_position_size(balance, symbol_info)
        if volume <= 0:
            return RiskDecision(False, "Calculated position size is zero")

        return RiskDecision(True, "ok", volume=volume)

    def compute_sl_tp(self, entry_price: float, side: str, pip_size: float) -> tuple[float, float]:
        distance_sl = self.stop_loss_pips * pip_size
        distance_tp = self.take_profit_pips * pip_size
        if side == "BUY":
            return entry_price - distance_sl, entry_price + distance_tp
        return entry_price + distance_sl, entry_price - distance_tp

    def compute_trailing_sl(
        self, side: str, entry_price: float, current_sl: float, current_price: float, pip_size: float
    ) -> float | None:
        """Returns a new stop-loss price once the trade is far enough in profit
        (breakeven_trigger_pips), optionally trailing behind by trailing_stop_pips
        as price keeps moving favorably. Returns None if no change is needed.
        The stop loss only ever moves in the profit-protecting direction.
        """
        if self.breakeven_trigger_pips <= 0:
            return None

        if side == "BUY":
            profit_pips = (current_price - entry_price) / pip_size
        else:
            profit_pips = (entry_price - current_price) / pip_size

        if profit_pips < self.breakeven_trigger_pips - 1e-6:  # tolerate float rounding at the exact boundary
            return None

        locked_pips = max(0.0, profit_pips - self.trailing_stop_pips) if self.trailing_stop_pips > 0 else 0.0

        if side == "BUY":
            candidate_sl = entry_price + locked_pips * pip_size
            return candidate_sl if candidate_sl > current_sl else None
        else:
            candidate_sl = entry_price - locked_pips * pip_size
            return candidate_sl if candidate_sl < current_sl else None
