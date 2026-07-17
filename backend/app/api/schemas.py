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
    poll_interval_seconds: int | None = None


class ModeUpdate(BaseModel):
    mode: str  # "demo" or "real"
    confirm: bool = False


class BacktestRequest(BaseModel):
    symbol: str = "EURUSD"
    period: str = "60d"
    interval: str = "15m"
    starting_balance: float = 10000.0
    risk_percent: float = 1.0
    stop_loss_pips: float = 20
    take_profit_pips: float = 40
