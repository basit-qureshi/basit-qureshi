from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

import pandas as pd


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class PendingType(str, Enum):
    """Stop orders only. A BUY STOP rests above the market and fills when price
    rises into it; a SELL STOP rests below and fills when price falls into it."""

    BUY_STOP = "BUY_STOP"
    SELL_STOP = "SELL_STOP"


@dataclass
class PendingOrder:
    ticket: str
    symbol: str
    order_type: PendingType
    volume: float
    price: float
    comment: str = ""


@dataclass
class AccountInfo:
    """What the broker says about the account.

    Every field that the engine is allowed to make a decision on is `None` when
    the broker did not supply it, never a convenient default. The previous
    model had no `free_margin` field at all, so the admission check read it with
    `getattr(account, "free_margin", None)` and silently passed every time.
    A missing value must block admission, not sail through it.
    """

    balance: float
    equity: float
    currency: str
    leverage: int
    account_id: str = "legacy"
    # "demo", "real", "contest" or "unknown" — the BROKER's own classification,
    # not the app's mode setting. They are different things and only this one
    # is authoritative.
    trade_mode: str = "unknown"
    # None means the broker did not say. Netting vs hedging changes what a
    # two-sided grid even costs in margin, so it is not assumed.
    hedging: bool | None = None
    free_margin: float | None = None
    margin: float | None = None
    margin_level: float | None = None
    # Whether the broker currently permits this account to trade at all.
    trade_allowed: bool | None = None
    broker_id: str | None = None

    @property
    def identity_known(self) -> bool:
        return self.trade_mode in ("demo", "real") and bool(self.account_id)


@dataclass
class Position:
    ticket: str
    symbol: str
    side: OrderSide
    volume: float
    open_price: float
    sl: float
    tp: float
    open_time: str
    profit: float
    magic: int = 0  # which program opened it; 0 means unknown/manual
    swap: float = 0.0
    commission: float = 0.0
    identifier: str | None = None
    costs_known: bool = True

    @property
    def net_profit(self):
        return (self.profit or 0.0) + self.swap + self.commission


@dataclass
class SymbolInfo:
    symbol: str
    pip_size: float  # price movement of one pip, e.g. 0.0001 for EURUSD
    pip_value_per_lot: float  # profit/loss per pip per 1.0 lot, in account currency
    min_volume: float
    volume_step: float
    #: The distance actually used to place the first grid level, in price units.
    #: It is the LARGER of a broker requirement and an application heuristic, and
    #: the three fields below say which is which. Do not read this field as "what
    #: the broker requires": it usually is not.
    min_stop_distance: float = 0.0
    spread: float = 0.0  # current ask - bid, in price units
    # --- where `min_stop_distance` comes from, kept separate ------------------
    # Conflating these hid an application choice behind a broker name. The
    # spread-multiple heuristic below is this app's invention, not a rule any
    # broker states, and on a wide spread it is usually the binding one — which
    # pushes every grid level further out and raises the completed-grid estimate.
    #: `trade_stops_level * point` exactly as the broker reports it, no buffer.
    broker_stop_level_distance: float = 0.0
    #: Extra distance THIS APPLICATION adds on top of the broker's figure, to
    #: survive rounding and price movement between calculation and submission.
    app_stop_buffer: float = 0.0
    #: This application's fallback for brokers that declare no minimum yet still
    #: reject a stop inside the live spread. A multiple of the spread.
    app_spread_multiple_distance: float = 0.0
    #: The multiple used above, so a reader does not have to divide to find it.
    app_spread_multiple: float = 0.0

    @property
    def stop_distance_binding(self) -> str:
        """Which input is actually setting the first grid step."""
        broker_side = self.broker_stop_level_distance + self.app_stop_buffer
        if not self.min_stop_distance:
            return "none"
        if self.app_spread_multiple_distance > broker_side:
            return "app_spread_multiple"
        if self.app_stop_buffer and self.broker_stop_level_distance:
            return "broker_stop_level_plus_app_buffer"
        return "broker_stop_level" if self.broker_stop_level_distance else "app_spread_multiple"

    def stop_distance_breakdown(self) -> dict:
        return {
            "effective_min_stop_distance": round(self.min_stop_distance, 6),
            "broker_stop_level_distance": round(self.broker_stop_level_distance, 6),
            "app_stop_buffer": round(self.app_stop_buffer, 6),
            "app_spread_multiple": self.app_spread_multiple,
            "app_spread_multiple_distance": round(self.app_spread_multiple_distance, 6),
            "binding": self.stop_distance_binding,
            "spread": round(self.spread, 6),
        }
    # Whether the broker's reported floating profit is already struck at the
    # executable closing side (bid for a long, ask for a short). When it is,
    # subtracting a further half-spread per position double-counts the exit.
    # None means nobody has verified it for this adapter, and an unverified
    # semantic must not be presented as a conservative guarantee.
    profit_includes_exit_spread: bool | None = None


class BrokerAdapter(ABC):
    """Common interface every broker (mock, MT5, future REST brokers) must implement."""

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    def get_account_info(self) -> AccountInfo: ...

    @abstractmethod
    def get_symbol_info(self, symbol: str) -> SymbolInfo: ...

    @abstractmethod
    def get_candles(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        """Returns a DataFrame indexed by time with columns: open, high, low, close, volume."""
        ...

    @abstractmethod
    def get_open_positions(self, symbol: str | None = None, magic: int | None = None) -> list[Position]:
        """Open positions, filtered to `magic` when given so the bot manages only
        its own trades and leaves manual ones alone."""
        ...

    @abstractmethod
    def place_order(
        self, symbol: str, side: OrderSide, volume: float, sl: float, tp: float
    ) -> Position: ...

    @abstractmethod
    def place_pending_order(
        self,
        symbol: str,
        order_type: PendingType,
        volume: float,
        price: float,
        comment: str = "",
        magic: int = 0,
    ) -> PendingOrder:
        """Rests a stop order at `price`. `magic` tags it so the bot can tell its
        own orders from anything placed by hand or by another program."""
        ...

    @abstractmethod
    def get_pending_orders(self, symbol: str | None = None, magic: int | None = None) -> list[PendingOrder]:
        """Orders still waiting to fill. Filtered to `magic` when given, so the
        bot never counts or cancels orders it did not place."""
        ...

    @abstractmethod
    def cancel_pending_order(self, ticket: str) -> None: ...

    @abstractmethod
    def close_position(self, ticket: str) -> float:
        """Closes the position and returns the realized profit."""
        ...

    @abstractmethod
    def get_current_price(self, symbol: str) -> float: ...

    @abstractmethod
    def modify_stop_loss(self, ticket: str, new_sl: float) -> None:
        """Moves an open position's stop loss (used for breakeven/trailing stop)."""
        ...

    @abstractmethod
    def modify_sl_tp(self, ticket: str, new_sl: float, new_tp: float) -> None:
        """Sets both stop loss and take profit — used to re-anchor them to the
        price the order actually filled at, rather than the price it was
        calculated from."""
        ...

    @abstractmethod
    def get_realized_profit(self, ticket: str) -> float | None:
        """Actual realized profit of a closed position (including commission/swap
        where the broker reports them), or None if the broker can't tell."""
        ...

    # --- optional capabilities ------------------------------------------------
    # Adapters that cannot provide these simply do not implement them. The
    # engine treats absence as "unknown" and refuses to admit new exposure on
    # it, rather than assuming a comfortable value.

    def calc_margin(self, symbol: str, side: OrderSide, volume: float, price: float) -> float | None:
        """Margin the broker would require for ONE proposed operation.

        MT5's order_calc_margin answers exactly this and nothing more: it does
        not account for positions already open or orders already resting. The
        caller must reconcile those separately. None means the broker could not
        be asked.
        """
        return None
