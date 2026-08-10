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
    # For Gold (2-digit quotes) one point = 0.01, so 100/200 is a $1.00 stop
    # against a $2.00 target — the 1:2 risk:reward used by the retest strategy.
    stop_loss_pips: float = 100
    take_profit_pips: float = 200
    max_open_trades: int = 1
    max_daily_loss_percent: float = 5.0
    max_daily_trades: int = 0  # 0 = unlimited
    breakeven_trigger_pips: float = 0  # 0 = disabled
    trailing_stop_pips: float = 0  # 0 = breakeven only, no further trailing
    # On M1 scalping a 30s poll only looks at the market twice per candle, so a
    # setup that appears and resolves inside one candle gets missed entirely.
    poll_interval_seconds: int = 5
    # Close a trade that has been open this long without hitting SL or TP, so
    # capital isn't parked in a setup that has stopped working. 0 = disabled.
    max_trade_minutes: int = 15
    ema_fast_period: int = 5  # lower = faster/more frequent signals, more noise
    ema_slow_period: int = 13
    # "retest_rejection" = M5 breakout, M1 retest + rejection entry (scalping, 1:2 RR)
    # "scalp_breakout"   = trend + breakout + volume, single timeframe
    # "ema_rsi"          = classic EMA crossover with RSI filter
    strategy: str = "retest_rejection"
    # How permissive the entry filters are: "aggressive" trades far more often
    # (each signal is weaker), "conservative" waits for cleaner setups.
    sensitivity: str = "balanced"

    database_url: str = "sqlite:///./trading_bot.db"


settings = Settings()
