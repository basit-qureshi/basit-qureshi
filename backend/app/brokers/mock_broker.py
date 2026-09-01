import time
import uuid
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from app.brokers.base import (
    AccountInfo,
    BrokerAdapter,
    OrderSide,
    PendingOrder,
    PendingType,
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
    # Gold quotes to 2 decimals, so one point is 0.01 and 0.01 lots is 1 ounce:
    # a $1 price move is $1 of P&L.
    "XAUUSD": 0.01,
    "XAUUSDm": 0.01,
}

# Money per lot per POINT — not per $1 of price. A standard gold lot is 100
# ounces and a point is 0.01, so one point on one lot is $1.00, and 0.01 lots
# earn a cent per point. Reading this as "per $1 of price" (100) overstates
# every result by a factor of a hundred.
_POINT_VALUE_PER_LOT = {"XAUUSD": 1.0, "XAUUSDm": 1.0}

# Points of movement per simulated step. Gold moves far more per minute than a
# forex major does, and a grid spaced in dollars looks absurdly quiet without it.
_STEP_POINTS = {"XAUUSD": 35.0, "XAUUSDm": 35.0}

_BASE_PRICES = {
    "EURUSD": 1.0850,
    "GBPUSD": 1.2650,
    "USDJPY": 156.50,
    "AUDUSD": 0.6550,
    "USDCHF": 0.8850,
    "USDCAD": 1.3650,
    "XAUUSD": 4200.00,
    "XAUUSDm": 4200.00,
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
        self._pending: dict[str, PendingOrder] = {}
        self._pending_magic: dict[str, int] = {}
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
        # Gold's spread is 24-30 points in practice, which is far too large to
        # model as "two pips" — the grid strategy pays it on every fill.
        spread = pip_size * 26 if symbol in _POINT_VALUE_PER_LOT else pip_size * 2
        return SymbolInfo(
            symbol=symbol,
            pip_size=pip_size,
            pip_value_per_lot=_POINT_VALUE_PER_LOT.get(symbol, 10.0),
            min_volume=0.01,
            volume_step=0.01,
            spread=spread,
        )

    def _ensure_history(self, symbol: str, count: int) -> None:
        if symbol in self._candle_cache:
            return
        base = _BASE_PRICES.get(symbol, 1.0)
        pip = _PIP_SIZES.get(symbol, 0.0001)
        n = max(count, 300)
        step = pip * _STEP_POINTS.get(symbol, 4.0)
        closes = base + np.cumsum(self._rng.normal(0, step, n))
        opens = np.roll(closes, 1)
        opens[0] = base
        highs = np.maximum(opens, closes) + np.abs(self._rng.normal(0, step / 2, n))
        lows = np.minimum(opens, closes) - np.abs(self._rng.normal(0, step / 2, n))
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
        step = pip * _STEP_POINTS.get(symbol, 4.0)
        df = self._candle_cache[symbol]
        last_close = df["close"].iloc[-1]
        new_close = last_close + self._rng.normal(0, step)
        new_open = last_close
        new_high = max(new_open, new_close) + abs(self._rng.normal(0, step / 2))
        new_low = min(new_open, new_close) - abs(self._rng.normal(0, step / 2))
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
        return pips_moved * pos.volume * _POINT_VALUE_PER_LOT.get(pos.symbol, 10.0)

    def get_open_positions(self, symbol: str | None = None, magic: int | None = None) -> list[Position]:
        self._trigger_pending()
        self._check_sl_tp()
        positions = list(self._positions.values())
        if symbol:
            positions = [p for p in positions if p.symbol == symbol]
        if magic is not None:
            positions = [p for p in positions if p.magic == magic]
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
                magic=p.magic,
            )
            for p in positions
        ]

    def _check_sl_tp(self) -> None:
        """Closes any position whose SL or TP has been crossed by the last known price."""
        for ticket in list(self._positions.keys()):
            pos = self._positions[ticket]
            current = self._prices.get(pos.symbol, pos.open_price)
            # 0 means "no level set", the same as MT5 — grid positions carry no
            # stop or target of their own and are exited by the basket rule.
            hit_sl = bool(pos.sl) and (
                (pos.side == OrderSide.BUY and current <= pos.sl)
                or (pos.side == OrderSide.SELL and current >= pos.sl)
            )
            hit_tp = bool(pos.tp) and (
                (pos.side == OrderSide.BUY and current >= pos.tp)
                or (pos.side == OrderSide.SELL and current <= pos.tp)
            )
            if hit_sl or hit_tp:
                self.close_position(ticket)

    def place_order(
        self, symbol: str, side: OrderSide, volume: float, sl: float, tp: float, magic: int = 0
    ) -> Position:
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
            magic=magic,
        )
        self._positions[ticket] = pos
        return pos

    def place_pending_order(
        self,
        symbol: str,
        order_type: PendingType,
        volume: float,
        price: float,
        comment: str = "",
        magic: int = 0,
    ) -> PendingOrder:
        self._ensure_history(symbol, 300)
        ticket = str(uuid.uuid4())[:8]
        order = PendingOrder(
            ticket=ticket, symbol=symbol, order_type=order_type, volume=volume,
            price=price, comment=comment,
        )
        self._pending[ticket] = order
        self._pending_magic[ticket] = magic
        return order

    def get_pending_orders(self, symbol: str | None = None, magic: int | None = None) -> list[PendingOrder]:
        self._trigger_pending()
        orders = list(self._pending.values())
        if symbol:
            orders = [o for o in orders if o.symbol == symbol]
        if magic is not None:
            orders = [o for o in orders if self._pending_magic.get(o.ticket) == magic]
        return orders

    def cancel_pending_order(self, ticket: str) -> None:
        self._pending.pop(ticket, None)
        self._pending_magic.pop(ticket, None)

    def _trigger_pending(self) -> None:
        """Fills any stop order the market has reached. A BUY STOP fills when
        price rises to it, a SELL STOP when price falls to it — and the fill
        pays the spread, exactly as a real stop order does."""
        for ticket in list(self._pending.keys()):
            order = self._pending[ticket]
            price = self._prices.get(order.symbol)
            if price is None:
                continue
            is_buy = order.order_type == PendingType.BUY_STOP
            reached = price >= order.price if is_buy else price <= order.price
            if not reached:
                continue
            spread = self.get_symbol_info(order.symbol).spread
            fill = order.price + spread / 2 if is_buy else order.price - spread / 2
            self._pending.pop(ticket)
            magic = self._pending_magic.pop(ticket, 0)
            pos_ticket = str(uuid.uuid4())[:8]
            self._positions[pos_ticket] = Position(
                ticket=pos_ticket,
                symbol=order.symbol,
                side=OrderSide.BUY if is_buy else OrderSide.SELL,
                volume=order.volume,
                open_price=fill,
                sl=0.0,
                tp=0.0,
                open_time=datetime.now(timezone.utc).isoformat(),
                profit=0.0,
                magic=magic,
            )

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

    def modify_sl_tp(self, ticket: str, new_sl: float, new_tp: float) -> None:
        pos = self._positions.get(ticket)
        if pos is not None:
            pos.sl = new_sl
            pos.tp = new_tp

    def get_realized_profit(self, ticket: str) -> float | None:
        return self._closed_profits.get(ticket)
