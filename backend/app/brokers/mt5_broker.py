import threading
from datetime import datetime, timezone
from functools import wraps

import pandas as pd

from app.brokers.base import (
    AccountInfo,
    BrokerAdapter,
    OrderSide,
    Position,
    SymbolInfo,
)

_TIMEFRAME_MAP = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 16385,
    "H4": 16388,
    "D1": 16408,
}


def _synchronized(method):
    """The MetaTrader5 package is not thread-safe — it talks to the terminal over
    a single shared IPC connection. FastAPI's sync routes run on threadpool
    threads while the trading engine's poll loop runs on the main thread, so
    without this lock, two concurrent MT5 calls can hang the process forever
    (requests stuck pending indefinitely) instead of erroring."""

    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class MT5Broker(BrokerAdapter):
    """Real broker connection via the MetaTrader5 terminal (works with Exness or any MT5 broker).

    Windows-only: the MetaTrader5 python package talks to a locally installed and
    logged-in MT5 terminal over a local IPC channel, there is no cloud/Linux equivalent.
    """

    def __init__(self, login: int, password: str, server: str):
        try:
            import MetaTrader5 as mt5
        except ImportError as exc:
            raise RuntimeError(
                "MetaTrader5 package not available. This broker only works on Windows "
                "with the MT5 terminal installed. Use BROKER_MODE=mock elsewhere."
            ) from exc
        self._mt5 = mt5
        self._login = login
        self._password = password
        self._server = server
        self._connected = False
        self._lock = threading.Lock()

    @_synchronized
    def connect(self) -> None:
        mt5 = self._mt5
        if not mt5.initialize(login=self._login, password=self._password, server=self._server):
            raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()}")
        self._connected = True

    @_synchronized
    def disconnect(self) -> None:
        self._mt5.shutdown()
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    @_synchronized
    def get_account_info(self) -> AccountInfo:
        info = self._mt5.account_info()
        if info is None:
            raise RuntimeError(f"MT5 account_info() failed: {self._mt5.last_error()}")
        return AccountInfo(balance=info.balance, equity=info.equity, currency=info.currency, leverage=info.leverage)

    def _ensure_symbol_selected(self, symbol: str) -> None:
        """Historical/tick data calls can fail with 'Terminal: Call failed' if the
        symbol isn't currently visible in Market Watch. Not lock-decorated itself —
        only ever called from inside an already-@_synchronized method (the lock
        isn't reentrant, so double-acquiring it here would deadlock)."""
        mt5 = self._mt5
        if mt5.symbol_info(symbol) is None:
            mt5.symbol_select(symbol, True)

    @_synchronized
    def get_symbol_info(self, symbol: str) -> SymbolInfo:
        mt5 = self._mt5
        self._ensure_symbol_selected(symbol)
        info = mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"Symbol {symbol} not found on this broker")
        pip_size = info.point * 10 if info.digits in (3, 5) else info.point
        tick_value = info.trade_tick_value or 1.0
        tick_size = info.trade_tick_size or info.point
        pip_value_per_lot = (pip_size / tick_size) * tick_value if tick_size else tick_value * 10
        # Broker's minimum SL/TP distance from the current price, in price units.
        # A tighter stop than this gets rejected with "Invalid stops" — Gold and
        # other non-forex-major symbols often require a much wider distance than
        # forex pairs do. A few extra points of buffer avoids rejections from
        # rounding/price movement between our calculation and the order reaching MT5.
        stops_level_points = getattr(info, "trade_stops_level", 0) or 0
        stops_level_distance = (stops_level_points + 5) * info.point if stops_level_points else 0.0
        # Some brokers report trade_stops_level=0 (no declared minimum) yet still
        # reject a stop that doesn't clear the live spread — Gold's spread alone
        # can be wider than a "normal" pip-based stop. Use whichever is larger.
        tick = mt5.symbol_info_tick(symbol)
        spread = (tick.ask - tick.bid) if tick else 0.0
        spread_based_distance = spread * 3
        min_stop_distance = max(stops_level_distance, spread_based_distance)
        return SymbolInfo(
            symbol=symbol,
            pip_size=pip_size,
            pip_value_per_lot=pip_value_per_lot,
            min_volume=info.volume_min,
            volume_step=info.volume_step,
            min_stop_distance=min_stop_distance,
            spread=spread,
        )

    @_synchronized
    def get_candles(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        mt5 = self._mt5
        self._ensure_symbol_selected(symbol)
        tf = _TIMEFRAME_MAP.get(timeframe, mt5.TIMEFRAME_M15)
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"MT5 copy_rates_from_pos failed for {symbol}: {mt5.last_error()}")
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df = df.set_index("time").rename(columns={"tick_volume": "volume"})
        return df[["open", "high", "low", "close", "volume"]]

    @_synchronized
    def get_current_price(self, symbol: str) -> float:
        self._ensure_symbol_selected(symbol)
        tick = self._mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(f"MT5 symbol_info_tick failed for {symbol}: {self._mt5.last_error()}")
        return (tick.bid + tick.ask) / 2

    @_synchronized
    def get_open_positions(self, symbol: str | None = None) -> list[Position]:
        mt5 = self._mt5
        raw = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        if raw is None:
            return []
        result = []
        for p in raw:
            side = OrderSide.BUY if p.type == mt5.POSITION_TYPE_BUY else OrderSide.SELL
            result.append(
                Position(
                    ticket=str(p.ticket),
                    symbol=p.symbol,
                    side=side,
                    volume=p.volume,
                    open_price=p.price_open,
                    sl=p.sl,
                    tp=p.tp,
                    open_time=datetime.fromtimestamp(p.time, tz=timezone.utc).isoformat(),
                    profit=p.profit,
                )
            )
        return result

    @_synchronized
    def place_order(self, symbol: str, side: OrderSide, volume: float, sl: float, tp: float) -> Position:
        mt5 = self._mt5
        tick = mt5.symbol_info_tick(symbol)
        price = tick.ask if side == OrderSide.BUY else tick.bid
        order_type = mt5.ORDER_TYPE_BUY if side == OrderSide.BUY else mt5.ORDER_TYPE_SELL
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 20,
            "magic": 990011,
            "comment": "forex-ai-bot",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            raise RuntimeError(f"MT5 order_send failed: {result}")
        return Position(
            ticket=str(result.order),
            symbol=symbol,
            side=side,
            volume=volume,
            open_price=price,
            sl=sl,
            tp=tp,
            open_time=datetime.now(timezone.utc).isoformat(),
            profit=0.0,
        )

    @_synchronized
    def close_position(self, ticket: str) -> float:
        mt5 = self._mt5
        positions = mt5.positions_get(ticket=int(ticket))
        if not positions:
            return 0.0
        pos = positions[0]
        tick = mt5.symbol_info_tick(pos.symbol)
        close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = tick.bid if pos.type == mt5.POSITION_TYPE_BUY else tick.ask
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": pos.volume,
            "type": close_type,
            "position": pos.ticket,
            "price": price,
            "deviation": 20,
            "magic": 990011,
            "comment": "forex-ai-bot-close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            raise RuntimeError(f"MT5 close order_send failed: {result}")
        return pos.profit

    @_synchronized
    def get_realized_profit(self, ticket: str) -> float | None:
        """Sums the actual deals MT5 recorded for this position — the same numbers
        the broker's own history shows — instead of estimating from balance moves."""
        mt5 = self._mt5
        try:
            deals = mt5.history_deals_get(position=int(ticket))
        except Exception:
            return None
        if not deals:
            return None
        total = sum(d.profit + d.commission + d.swap for d in deals)
        return round(total, 2)

    @_synchronized
    def modify_stop_loss(self, ticket: str, new_sl: float) -> None:
        self._send_sltp(ticket, new_sl, None)

    @_synchronized
    def modify_sl_tp(self, ticket: str, new_sl: float, new_tp: float) -> None:
        self._send_sltp(ticket, new_sl, new_tp)

    def _send_sltp(self, ticket: str, new_sl: float, new_tp: float | None) -> None:
        """Not lock-decorated — only called from inside @_synchronized methods
        (the lock isn't reentrant, so acquiring it again here would deadlock)."""
        mt5 = self._mt5
        positions = mt5.positions_get(ticket=int(ticket))
        if not positions:
            return
        pos = positions[0]
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": pos.symbol,
            "position": pos.ticket,
            "sl": new_sl,
            "tp": pos.tp if new_tp is None else new_tp,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            raise RuntimeError(f"MT5 modify SL/TP failed: {result}")
