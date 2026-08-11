from pydantic import BaseModel


class StartRequest(BaseModel):
    confirm_real: bool = False


class SettingsUpdate(BaseModel):
    symbol: str | None = None
    timeframe: str | None = None
    risk_percent: float | None = None
    stop_loss_pips: float | None = None
    take_profit_pips: float | None = None
    max_open_trades: int | None = None
    max_daily_loss_percent: float | None = None
    max_daily_trades: int | None = None
    breakeven_trigger_pips: float | None = None
    trailing_stop_pips: float | None = None
    poll_interval_seconds: int | None = None
    ema_fast_period: int | None = None
    ema_slow_period: int | None = None
    strategy: str | None = None
    sensitivity: str | None = None
    max_trade_minutes: int | None = None
    quick_profit_usd: float | None = None
    max_spread_points: float | None = None
    trend_filter_timeframe: str | None = None


class ModeUpdate(BaseModel):
    mode: str  # "demo" or "real"
    confirm: bool = False


class TestOrderRequest(BaseModel):
    side: str  # "BUY" or "SELL"
    volume: float = 0.01
    confirm_real: bool = False


class BacktestRequest(BaseModel):
    symbol: str = "EURUSD"
    period: str = "60d"
    interval: str = "15m"
    starting_balance: float = 10000.0
    risk_percent: float = 1.0
    stop_loss_pips: float = 100
    take_profit_pips: float = 200
    strategy: str = "retest_rejection"
    sensitivity: str = "balanced"
