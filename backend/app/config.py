from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    broker_mode: str = "mock"  # "mock" or "mt5"

    mt5_login: int | None = None
    mt5_password: str | None = None
    mt5_server: str | None = None

    @field_validator("mt5_login", "mt5_password", "mt5_server", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        return None if v == "" else v

    account_type: str = "demo"  # "demo" or "real"

    symbol: str = "XAUUSD"  # Gold. Use your broker's exact name, e.g. Exness Standard uses "XAUUSDm".
    timeframe: str = "M1"
    risk_percent: float = 1.0
    stop_loss_pips: float = 20
    take_profit_pips: float = 40
    max_open_trades: int = 1
    max_daily_loss_percent: float = 5.0
    max_daily_trades: int = 0  # 0 = unlimited
    breakeven_trigger_pips: float = 0  # 0 = disabled
    trailing_stop_pips: float = 0  # 0 = breakeven only, no further trailing
    poll_interval_seconds: int = 30
    ema_fast_period: int = 5  # lower = faster/more frequent signals, more noise
    ema_slow_period: int = 13
    strategy: str = "scalp_breakout"  # "ema_rsi" (crossover) or "scalp_breakout" (trend+breakout+volume)

    database_url: str = "sqlite:///./trading_bot.db"


settings = Settings()
