"""Deterministic test doubles for the grid engine.

The mock broker drives its own clock and price walk, which is right for
exercising the app end to end but wrong for asserting candle-boundary rules.
FakeBroker gives the test explicit control of both the price and the M1 candle
timestamp, so "same candle" and "next candle" are stated rather than timed.
"""

import os
import tempfile

os.environ.setdefault("DATABASE_URL", "sqlite:///" + tempfile.mktemp(suffix=".db"))

import uuid
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.brokers.base import (
    AccountInfo,
    BrokerAdapter,
    OrderSide,
    PendingOrder,
    PendingType,
    Position,
    SymbolInfo,
)

POINT = 0.01
VALUE_PER_POINT_PER_LOT = 1.0  # gold: one point on one lot is $1
MAGIC = 990022


class FakeBroker(BrokerAdapter):
    def __init__(self, price: float = 4000.0, balance: float = 1000.0):
        self.price = price
        self.balance = balance
        self.candle_time = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
        self.candles_available = True
        self.positions: dict[str, Position] = {}
        self.pending: dict[str, PendingOrder] = {}
        self.pending_magic: dict[str, int] = {}
        self.realized: dict[str, float] = {}
        self.spread = 0.24

    # -- test controls --------------------------------------------------------
    def next_candle(self, minutes: int = 1) -> None:
        self.candle_time += timedelta(minutes=minutes)

    def open_position(self, side: str, entry: float, magic: int = MAGIC, volume: float = 0.01) -> Position:
        ticket = str(uuid.uuid4())[:8]
        pos = Position(
            ticket=ticket, symbol="XAUUSD", side=OrderSide(side), volume=volume,
            open_price=entry, sl=0.0, tp=0.0,
            open_time=datetime.now(timezone.utc).isoformat(), profit=0.0, magic=magic,
        )
        self.positions[ticket] = pos
        return pos

    # -- broker interface -----------------------------------------------------
    def connect(self): pass
    def disconnect(self): pass
    def is_connected(self): return True

    def get_account_info(self):
        equity = self.balance + sum(self._profit(p) for p in self.positions.values())
        return AccountInfo(balance=self.balance, equity=equity, currency="USD", leverage=500)

    def get_symbol_info(self, symbol):
        return SymbolInfo(symbol, POINT, VALUE_PER_POINT_PER_LOT, 0.01, 0.01,
                          min_stop_distance=0.0, spread=self.spread)

    def get_candles(self, symbol, timeframe, count):
        if not self.candles_available:
            raise RuntimeError("no candle data")
        times = [self.candle_time - timedelta(minutes=count - 1 - i) for i in range(count)]
        return pd.DataFrame(
            {"open": self.price, "high": self.price, "low": self.price,
             "close": self.price, "volume": 100.0},
            index=pd.DatetimeIndex(times),
        )

    def _profit(self, pos: Position) -> float:
        moved = (self.price - pos.open_price) / POINT
        if pos.side == OrderSide.SELL:
            moved = -moved
        return round(moved * pos.volume * VALUE_PER_POINT_PER_LOT, 2)

    def get_open_positions(self, symbol=None, magic=None):
        self._fill_pending()
        out = []
        for p in self.positions.values():
            if symbol and p.symbol != symbol:
                continue
            if magic is not None and p.magic != magic:
                continue
            out.append(
                Position(p.ticket, p.symbol, p.side, p.volume, p.open_price, p.sl, p.tp,
                         p.open_time, self._profit(p), p.magic)
            )
        return out

    def place_order(self, symbol, side, volume, sl, tp, magic=0):
        return self.open_position(side.value, self.price, magic=magic, volume=volume)

    def place_pending_order(self, symbol, order_type, volume, price, comment="", magic=0):
        ticket = str(uuid.uuid4())[:8]
        order = PendingOrder(ticket, symbol, order_type, volume, price, comment)
        self.pending[ticket] = order
        self.pending_magic[ticket] = magic
        return order

    def get_pending_orders(self, symbol=None, magic=None):
        self._fill_pending()
        out = []
        for o in self.pending.values():
            if symbol and o.symbol != symbol:
                continue
            if magic is not None and self.pending_magic.get(o.ticket) != magic:
                continue
            out.append(o)
        return out

    def cancel_pending_order(self, ticket):
        self.pending.pop(ticket, None)
        self.pending_magic.pop(ticket, None)

    def _fill_pending(self):
        for ticket in list(self.pending):
            o = self.pending[ticket]
            is_buy = o.order_type == PendingType.BUY_STOP
            if (is_buy and self.price >= o.price) or (not is_buy and self.price <= o.price):
                magic = self.pending_magic.pop(ticket, 0)
                self.pending.pop(ticket)
                fill = o.price + self.spread / 2 if is_buy else o.price - self.spread / 2
                self.open_position("BUY" if is_buy else "SELL", fill, magic=magic, volume=o.volume)

    def close_position(self, ticket):
        pos = self.positions.pop(ticket, None)
        if pos is None:
            return 0.0
        profit = self._profit(pos)
        self.balance = round(self.balance + profit, 2)
        self.realized[ticket] = profit
        return profit

    def get_current_price(self, symbol):
        return self.price

    def modify_stop_loss(self, ticket, new_sl): pass
    def modify_sl_tp(self, ticket, new_sl, new_tp): pass
    def get_realized_profit(self, ticket):
        return self.realized.get(ticket)


@pytest.fixture(autouse=True)
def clean_db():
    """Each test gets its own database file, so daily accounting in one test
    can never leak into another."""
    from app import db as db_module
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    path = tempfile.mktemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    db_module.engine = engine
    db_module.SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    db_module.init_db()
    yield
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def broker():
    return FakeBroker()


@pytest.fixture
def engine_factory(broker):
    from app.engine.grid_engine import GridEngine

    def _make(**kw):
        kw.setdefault("magic_number", MAGIC)
        return GridEngine(broker=broker, symbol="XAUUSD", mode="demo", **kw)

    return _make
