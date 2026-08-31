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
    # Trade a fixed lot size instead of sizing from risk_percent. 0 = size from
    # risk. A fixed size ignores the stop distance, so the money at risk moves
    # around with it — handy for testing, worse for consistent risk.
    fixed_lot_size: float = 0
    # For Gold (2-digit quotes) one point = 0.01, so 100/200 is a $1.00 stop
    # against a $2.00 target — the 1:2 risk:reward used by the retest strategy.
    stop_loss_pips: float = 100
    take_profit_pips: float = 200
    # Several scalps can run at once. Note each one risks risk_percent of the
    # balance, so total exposure is roughly max_open_trades x risk_percent.
    max_open_trades: int = 3
    max_daily_loss_percent: float = 5.0
    max_daily_trades: int = 0  # 0 = unlimited
    # Once a trade is this far in profit the stop moves to entry, so it can no
    # longer turn into a loss. Set at 1R (= stop_loss_pips) so the move happens
    # only after the trade has proven itself.
    #
    # trailing_stop_pips deliberately defaults to 0 (breakeven only, no trail).
    # Measured over 3000 simulated M1 gold candles, a tight trail looked good on
    # win rate but was much worse on money, because it kept closing winners long
    # before the 200-point target:
    #     breakeven/trail   win%   avg win   net points
    #     60 / 30           75.5%      108      +27,600
    #     60 / 60           75.5%      150      +42,856
    #     100 / 0           70.7%      200      +49,300  <- best
    breakeven_trigger_pips: float = 100  # 0 = disabled
    trailing_stop_pips: float = 0  # 0 = breakeven only, no further trailing
    # Close a trade the moment its floating profit reaches this many account
    # currency units, regardless of where TP sits. 0 = let TP do its job.
    # Taking profit earlier than TP shrinks the reward side of the risk:reward
    # ratio — a $2 exit against a $3 stop is 1:0.67, not 1:2.
    quick_profit_usd: float = 0
    # Skip entries while the spread is wider than this many points — spread is
    # paid on every scalp, and it blows out during news and thin hours.
    # 0 = trade regardless. Gold normally sits around 20-30 points.
    max_spread_points: float = 45
    # --- Basket (multi-entry) scalping ---------------------------------------
    # Instead of one trade with its own TP, stack several entries in the same
    # direction and manage them as one group: close the whole basket when its
    # COMBINED floating profit hits basket_target_usd.
    #
    # Be clear-eyed about what this is. Adding to a position that is moving
    # against you produces a long run of small wins and then one very large
    # loss when price keeps going — the small wins are not evidence it works,
    # they are what this shape looks like before the loss arrives.
    # basket_max_loss_usd is therefore not optional decoration: it is the only
    # thing standing between this mode and an account-sized loss.
    basket_mode: bool = False
    basket_max_entries: int = 5
    basket_add_gap_points: float = 50  # price must move this far against the last entry before adding
    basket_target_usd: float = 3.0  # close the whole group at this combined profit
    basket_max_loss_usd: float = 15.0  # hard group stop — close everything, take the loss
    # Only take a signal when this timeframe's trend agrees with it. Counter-trend
    # pushes are usually pullback noise, and they are what chops an account.
    # Measured on simulated M1 gold, M5 gave the best result of the options
    # (H1 was worse than no filter at all — too slow to describe an M1 scalp).
    # "" = no filter.
    trend_filter_timeframe: str = "M5"
    # On M1 scalping a 30s poll only looks at the market twice per candle, so a
    # setup that appears and resolves inside one candle gets missed entirely.
    poll_interval_seconds: int = 5
    # Close a trade that has been open this long without hitting SL or TP, so
    # capital isn't parked in a setup that has stopped working. 0 = disabled.
    max_trade_minutes: int = 15
    ema_fast_period: int = 5  # lower = faster/more frequent signals, more noise
    ema_slow_period: int = 13
    # "smc" = Smart Money Concepts: M15 BOS -> order block + FVG -> M1 market
    # structure shift -> M5 entry zone. This is the only strategy offered in the
    # dashboard; the older signal strategies still exist in the code and can be
    # named here, but they are not selectable and are not maintained.
    strategy: str = "smc"

    # --- SMC settings ---------------------------------------------------------
    # How far beyond the M5 entry zone the stop sits, on top of the live spread.
    # A stop resting exactly on the zone's edge gets taken out by the spread and
    # a single wick before the move it was placed for.
    smc_sl_buffer_points: float = 20
    # Minimum reward:risk before a setup is accepted at all. The structural
    # target is wherever the BOS ran to, so this is what stops the bot taking
    # setups whose target is too close to be worth the stop.
    smc_min_rr: float = 2.0
    # If price never comes back for the entry and instead runs this far past the
    # M1 shift, take the trade at market rather than watch the move go without
    # it — but only while at least smc_fallback_min_rr remains.
    smc_fallback_points: float = 150
    smc_fallback_min_rr: float = 1.0
    # Drop a planned setup that has not filled within this many minutes. The
    # structure it was read from goes stale.
    smc_setup_expiry_minutes: int = 45
    # How permissive the entry filters are: "aggressive" trades far more often
    # (each signal is weaker), "conservative" waits for cleaner setups.
    sensitivity: str = "balanced"

    database_url: str = "sqlite:///./trading_bot.db"


settings = Settings()
