from pydantic import BaseModel


class StartRequest(BaseModel):
    confirm_real: bool = False


class SettingsUpdate(BaseModel):
    symbol: str | None = None
    timeframe: str | None = None
    poll_interval_seconds: int | None = None
    grid_lot_size: float | None = None
    grid_buy_stop_levels: int | None = None
    grid_sell_stop_levels: int | None = None
    grid_distance: float | None = None
    grid_basket_take_profit_usd: float | None = None
    grid_basket_stop_loss_usd: float | None = None
    grid_max_open_positions: int | None = None
    grid_max_daily_loss_usd: float | None = None
    grid_max_equity_drawdown_percent: float | None = None
    grid_magic_number: int | None = None
    grid_trading_start_hour: int | None = None
    grid_trading_end_hour: int | None = None


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
    basket_stop_loss_usd: float = 0.0
    # Leaving this at 0 makes the result fiction: the grid pays a spread on
    # every single fill, and gold's is 24-30 points.
    spread_points: float = 24.0
    max_daily_loss_usd: float = 100.0
    max_equity_drawdown_percent: float = 30.0
