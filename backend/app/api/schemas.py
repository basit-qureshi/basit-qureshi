from pydantic import BaseModel


class StartRequest(BaseModel):
    confirm_real: bool = False


class SettingsUpdate(BaseModel):
    symbol: str | None = None
    timeframe: str | None = None
    risk_percent: float | None = None
    fixed_lot_size: float | None = None
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
    basket_mode: bool | None = None
    basket_max_entries: int | None = None
    basket_add_gap_points: float | None = None
    basket_target_usd: float | None = None
    basket_max_loss_usd: float | None = None
    smc_sl_buffer_points: float | None = None
    smc_min_rr: float | None = None
    smc_fallback_points: float | None = None
    smc_fallback_min_rr: float | None = None
    smc_setup_expiry_minutes: int | None = None


class ModeUpdate(BaseModel):
    mode: str  # "demo" or "real"
    confirm: bool = False


class TestOrderRequest(BaseModel):
    side: str  # "BUY" or "SELL"
    volume: float = 0.01
    confirm_real: bool = False


class BacktestRequest(BaseModel):
    symbol: str = "XAUUSD"
    period: str = "7d"
    interval: str = "1m"
    starting_balance: float = 10000.0
    risk_percent: float = 1.0
    stop_loss_pips: float = 100
    take_profit_pips: float = 200
    strategy: str = "smc"
    sensitivity: str = "balanced"
    fixed_lot_size: float = 0
    basket_mode: bool = False
    basket_max_entries: int = 5
    basket_add_gap_points: float = 50
    basket_target_usd: float = 3.0
    basket_max_loss_usd: float = 15.0
    basket_max_bars: int = 0  # age limit in bars, 0 = no time exit
    # Cost charged on every entry. Leaving it at 0 makes any high-frequency
    # result meaningless; gold typically sits around 20-30 points.
    spread_points: float = 24
