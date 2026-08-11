import time
import uuid
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from app.brokers.base import (
    AccountInfo,
    BrokerAdapter,
    OrderSide,
    Position,
    SymbolInfo,
)

# Rough pip sizes for common pairs so the simulator behaves realistically.
_PIP_SIZES = {
    "EURUSD": 0.0001,
    "GBPUSD": 0.0001,
    "USDJPY": 0.01,
    "AUDUSD": 0.0001,
    "USDCHF": 0.0001,
    "USDCAD": 0.0001,
}

_BASE_PRICES = {
    "EURUSD": 1.0850,
    "GBPUSD": 1.2650,
    "USDJPY": 156.50,
    "AUDUSD": 0.6550,
    "USDCHF": 0.8850,
    "USDCAD": 1.3650,
}


class MockBroker(BrokerAdapter):
    """Simulated broker: fake price feed + fake balance/positions.

    Runs anywhere with no external dependency, so the whole bot can be
    exercised end-to-end before ever touching a real Exness/MT5 account.

    Price only ever changes in one place (`_advance`) so every other method
    (current price, SL/TP checks, unrealized P&L) reads a single consistent
    price instead of each doing its own random walk. `_advance` is throttled
    to real wall-clock time (`_advance_interval_seconds`) rather than firing
    on every `get_candles` call, so reading candles from multiple places at
    once (the trading engine's poll loop, a chart polling for live updates)
    never over-drives the simulated market — same as a real broker, where
    time passes on its own regardless of how often you check the price.
    """

    def __init__(self, starting_balance: float = 10_000.0, currency: str = "USD", leverage: int = 100):
        self._connected = False
        self._balance = starting_balance
        self._currency = currency
        self._leverage = leverage
        self._positions: dict[str, Position] = {}
        self._closed_profits: dict[str, float] = {}
        self._prices: dict[str, float] = {}
        self._candle_cache: dict[str, pd.DataFrame] = {}
        self._last_advance_time: dict[str, float] = {}
        self._advance_interval_seconds = 5.0
        self._rng = np.random.default_rng(7)

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def get_account_info(self) -> AccountInfo:
        equity = self._balance + sum(self._unrealized_profit(p) for p in self._positions.values())
        return AccountInfo(balance=self._balance, equity=equity, currency=self._currency, leverage=self._leverage)

    def get_symbol_info(self, symbol: str) -> SymbolInfo:
        pip_size = _PIP_SIZES.get(symbol, 0.0001)
        return SymbolInfo(
            symbol=symbol,
            pip_size=pip_size,
            pip_value_per_lot=10.0,  # standard approximation for a $-quoted account, 1 standard lot
            min_volume=0.01,
            volume_step=0.01,
            spread=pip_size * 2,  # a plausible tight spread for the simulator
        )

    def _ensure_history(self, symbol: str, count: int) -> None:
        if symbol in self._candle_cache:
            return
        base = _BASE_PRICES.get(symbol, 1.0)
        pip = _PIP_SIZES.get(symbol, 0.0001)
        n = max(count, 300)
        closes = base + np.cumsum(self._rng.normal(0, pip * 4, n))
        opens = np.roll(closes, 1)
        opens[0] = base
        highs = np.maximum(opens, closes) + np.abs(self._rng.normal(0, pip * 2, n))
        lows = np.minimum(opens, closes) - np.abs(self._rng.normal(0, pip * 2, n))
        idx = pd.date_range(end=pd.Timestamp.utcnow(), periods=n, freq="15min")
        df = pd.DataFrame(
            {"open": opens, "high": highs, "low": lows, "close": closes, "volume": self._rng.integers(100, 1000, n)},
            index=idx,
        )
        self._candle_cache[symbol] = df
        self._prices[symbol] = float(df["close"].iloc[-1])

    def _advance(self, symbol: str) -> None:
        """Appends exactly one new synthetic candle, moving the market forward by one step."""
        pip = _PIP_SIZES.get(symbol, 0.0001)
        df = self._candle_cache[symbol]
        last_close = df["close"].iloc[-1]
        new_close = last_close + self._rng.normal(0, pip * 4)
        new_open = last_close
        new_high = max(new_open, new_close) + abs(self._rng.normal(0, pip * 2))
        new_low = min(new_open, new_close) - abs(self._rng.normal(0, pip * 2))
        new_row = pd.DataFrame(
            {"open": [new_open], "high": [new_high], "low": [new_low], "close": [new_close], "volume": [500]},
            index=[pd.Timestamp.utcnow()],
        )
        self._candle_cache[symbol] = pd.concat([df, new_row]).iloc[-2000:]
        self._prices[symbol] = float(new_close)

    def get_current_price(self, symbol: str) -> float:
        self._ensure_history(symbol, 300)
        return self._prices[symbol]

    def get_candles(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        self._ensure_history(symbol, count)
        now = time.time()
        if now - self._last_advance_time.get(symbol, 0.0) >= self._advance_interval_seconds:
            self._advance(symbol)
            self._last_advance_time[symbol] = now
        return self._candle_cache[symbol].tail(count)

    def _unrealized_profit(self, pos: Position) -> float:
        current = self._prices.get(pos.symbol, pos.open_price)
        pip = _PIP_SIZES.get(pos.symbol, 0.0001)
        pips_moved = (current - pos.open_price) / pip
        if pos.side == OrderSide.SELL:
            pips_moved = -pips_moved
        return pips_moved * pos.volume * 10.0  # ~$10/pip per standard lot approximation

    def get_open_positions(self, symbol: str | None = None) -> list[Position]:
        self._check_sl_tp()
        positions = list(self._positions.values())
        if symbol:
            positions = [p for p in positions if p.symbol == symbol]
        return [
            Position(
                ticket=p.ticket,
                symbol=p.symbol,
                side=p.side,
                volume=p.volume,
                open_price=p.open_price,
                sl=p.sl,
                tp=p.tp,
                open_time=p.open_time,
                profit=self._unrealized_profit(p),
            )
            for p in positions
        ]

    def _check_sl_tp(self) -> None:
        """Closes any position whose SL or TP has been crossed by the last known price."""
        for ticket in list(self._positions.keys()):
            pos = self._positions[ticket]
            current = self._prices.get(pos.symbol, pos.open_price)
            hit_sl = (pos.side == OrderSide.BUY and current <= pos.sl) or (
                pos.side == OrderSide.SELL and current >= pos.sl
            )
            hit_tp = (pos.side == OrderSide.BUY and current >= pos.tp) or (
                pos.side == OrderSide.SELL and current <= pos.tp
            )
            if hit_sl or hit_tp:
                self.close_position(ticket)

    def place_order(self, symbol: str, side: OrderSide, volume: float, sl: float, tp: float) -> Position:
        price = self.get_current_price(symbol)
        ticket = str(uuid.uuid4())[:8]
        pos = Position(
            ticket=ticket,
            symbol=symbol,
            side=side,
            volume=volume,
            open_price=price,
            sl=sl,
            tp=tp,
            open_time=datetime.now(timezone.utc).isoformat(),
            profit=0.0,
        )
        self._positions[ticket] = pos
        return pos

    def close_position(self, ticket: str) -> float:
        pos = self._positions.pop(ticket, None)
        if pos is None:
            return 0.0
        profit = self._unrealized_profit(pos)
        self._balance += profit
        self._closed_profits[ticket] = round(profit, 2)
        return profit

    def modify_stop_loss(self, ticket: str, new_sl: float) -> None:
        pos = self._positions.get(ticket)
        if pos is not None:
            pos.sl = new_sl

    def get_realized_profit(self, ticket: str) -> float | None:
        return self._closed_profits.get(ticket)
