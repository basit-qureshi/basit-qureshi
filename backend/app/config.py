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
    timeframe: str = "M1"  # the grid is an M1 strategy; this also drives the chart

    # The bot runs one strategy: the XAUUSD M1 pending-order grid. Nothing else
    # remains, so this exists only to make what is running explicit.
    strategy: str = "grid"

    # How often the grid is checked. The basket target is a floor, so a slow
    # poll means closing later than $10 rather than at it.
    poll_interval_seconds: int = 5

    # --- Grid strategy --------------------------------------------------------
    # Fixed lot on every order. It is deliberately never scaled after a loss:
    # martingale is what turns a run of small losses into a blown account.
    grid_lot_size: float = 0.01
    grid_buy_stop_levels: int = 10
    grid_sell_stop_levels: int = 10
    # Spacing between levels, in price. 0.30 on gold is 30 points.
    grid_distance: float = 0.30
    # Close the WHOLE basket the moment the combined net profit of every open
    # grid position reaches this. Not per trade — the total.
    grid_basket_take_profit_usd: float = 10.0
    # Stop trading for the rest of the broker day once this much NET REALIZED
    # profit has been booked. 0 = off, and it is deliberately off by default:
    # the amount is the user's decision, not a number to guess for them.
    # Realized means settled trades after their losses, commission and swap -
    # never floating profit, never equity movement.
    grid_daily_profit_target_usd: float = 0.0
    # Optional mirror of the target on the losing side: close the basket at this
    # combined loss. 0 = off, which is the strategy exactly as specified. Off
    # means a losing basket is bounded only by the two limits below.
    grid_basket_stop_loss_usd: float = 0.0
    grid_max_open_positions: int = 20
    # These two are the entire risk model. A grid carries no per-trade stop, so
    # nothing else ends a basket that keeps going the wrong way. At 0.01 lots a
    # fully filled 10+10 grid is 0.20 lots, and on gold that is $20 of profit or
    # loss for every $1 the price moves.
    grid_max_daily_loss_usd: float = 100.0
    grid_max_equity_drawdown_percent: float = 30.0
    # Tags every order so the bot manages only its own, leaving manual trades
    # and any other program alone.
    grid_magic_number: int = 990022
    # UTC hours. 0-24 means trade around the clock.
    grid_trading_start_hour: int = 0
    grid_trading_end_hour: int = 24

    # Timezone used to decide which broker trading day a candle belongs to, and
    # to show times on the dashboard. Broker candle stamps arrive without a
    # zone, so they are read as UTC and converted here; set this to your
    # broker's zone if its day should roll over at a different hour.
    timezone: str = "Asia/Karachi"

    database_url: str = "sqlite:///./trading_bot.db"


settings = Settings()
