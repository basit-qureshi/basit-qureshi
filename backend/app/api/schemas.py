from pydantic import BaseModel, Field, ConfigDict, field_validator


class StartRequest(BaseModel):
    confirm_real: bool = False


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    symbol: str | None = None
    timeframe: str | None = None
    poll_interval_seconds: float | None = Field(None, ge=0.1, le=60)
    grid_lot_size: float | None = Field(None, ge=0.01, le=100)
    grid_buy_stop_levels: int | None = Field(None, ge=1, le=100)
    grid_sell_stop_levels: int | None = Field(None, ge=1, le=100)
    grid_distance: float | None = Field(None, ge=0.01, le=100)
    grid_basket_take_profit_usd: float | None = Field(None, ge=0.01, le=100000)
    grid_daily_profit_target_usd: float | None = None
    grid_basket_stop_loss_usd: float | None = Field(None, ge=0, le=100000)
    grid_max_open_positions: int | None = Field(None, ge=1, le=200)
    grid_max_daily_loss_usd: float | None = Field(None, ge=0, le=100000)
    grid_max_equity_drawdown_percent: float | None = Field(None, ge=0, le=100)
    grid_magic_number: int | None = Field(None, ge=1, le=2147483647)
    grid_trading_start_hour: int | None = Field(None, ge=0, le=23)
    grid_trading_end_hour: int | None = Field(None, ge=0, le=24)
    timezone: str | None = None


    @field_validator("symbol")
    @classmethod
    def _gold_symbol(cls, value):
        if value is not None and not value.upper().startswith("XAUUSD"):
            raise ValueError("This build requires an XAUUSD broker symbol")
        return value

    @field_validator("timezone")
    @classmethod
    def _timezone_valid(cls, value):
        if value is not None:
            from zoneinfo import ZoneInfo
            try:
                ZoneInfo(value)
            except Exception as exc:
                raise ValueError("Unknown timezone") from exc
        return value

    @field_validator("timeframe")
    @classmethod
    def _m1_only(cls, value):
        if value is not None and value != "M1":
            raise ValueError("The grid and AI currently require M1")
        return value

    @field_validator("grid_daily_profit_target_usd")
    @classmethod
    def _target_not_negative(cls, v):
        # Zero disables the target; a negative one would halt trading the moment
        # the day opened, which is never what anyone means.
        if v is not None and v < 0:
            raise ValueError("Daily profit target cannot be negative (use 0 to switch it off)")
        return v


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
    lot_size: float = 0.01
    buy_stop_levels: int = 10
    sell_stop_levels: int = 10
    grid_distance: float = 0.30
    basket_take_profit_usd: float = 10.0
    daily_profit_target_usd: float = 0.0
    basket_stop_loss_usd: float = 0.0
    # Leaving this at 0 makes the result fiction: the grid pays a spread on
    # every single fill, and gold's is 24-30 points.
    spread_points: float = 24.0
    max_daily_loss_usd: float = 100.0
    max_equity_drawdown_percent: float = 30.0
