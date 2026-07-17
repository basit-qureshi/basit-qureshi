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

    symbol: str = "EURUSD"
    timeframe: str = "M15"
    risk_percent: float = 1.0
    stop_loss_pips: float = 20
    take_profit_pips: float = 40
    max_open_trades: int = 1
    max_daily_loss_percent: float = 5.0
    poll_interval_seconds: int = 30

    database_url: str = "sqlite:///./trading_bot.db"


settings = Settings()
