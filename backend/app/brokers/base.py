from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

import pandas as pd


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class AccountInfo:
    balance: float
    equity: float
    currency: str
    leverage: int


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


@dataclass
class SymbolInfo:
    symbol: str
    pip_size: float  # price movement of one pip, e.g. 0.0001 for EURUSD
    pip_value_per_lot: float  # profit/loss per pip per 1.0 lot, in account currency
    min_volume: float
    volume_step: float
    min_stop_distance: float = 0.0  # broker's minimum SL/TP distance from price, in price units


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
    def get_open_positions(self, symbol: str | None = None) -> list[Position]: ...

    @abstractmethod
    def place_order(
        self, symbol: str, side: OrderSide, volume: float, sl: float, tp: float
    ) -> Position: ...

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
