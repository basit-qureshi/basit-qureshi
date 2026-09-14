"""Replay recorded Bid/Ask ticks through the live GridEngine in an isolated database."""
import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import db
from app.brokers.mock_broker import MockBroker
from app.brokers.base import OrderSide, Position
from app.engine.grid_engine import GridEngine


class ReplayBroker(MockBroker):
    def __init__(self, symbol, balance, contract_size):
        super().__init__(balance)
        self.symbol = symbol
        self.contract_size = contract_size
        self.bid = self.ask = 0.0
        self.when = None
        self.bars = {}
        self.counter = 0

    def step(self, row):
        self.when, self.bid, self.ask = row.time, float(row.bid), float(row.ask)
        self._prices[self.symbol] = (self.bid + self.ask) / 2
        stamp = self.when.floor("min")
        mid = (self.bid + self.ask) / 2
        if stamp not in self.bars:
            self.bars[stamp] = [mid, mid, mid, mid, 1]
        else:
            bar = self.bars[stamp]
            bar[1], bar[2], bar[3], bar[4] = max(bar[1], mid), min(bar[2], mid), mid, bar[4] + 1
        self._trigger_pending()  # Broker fills happen on every tick, independently of polling.

    def get_candles(self, symbol, timeframe, count):
        return pd.DataFrame.from_dict(self.bars, orient="index",
                columns=["open", "high", "low", "close", "volume"]).tail(count)

    def get_current_price(self, symbol):
        return (self.bid + self.ask) / 2

    def _ensure_history(self, symbol, count):
        pass

    def get_symbol_info(self, symbol):
        info = super().get_symbol_info(symbol)
        info.spread = self.ask - self.bid
        return info

    def _unrealized_profit(self, position):
        move = self.bid - position.open_price if position.side == OrderSide.BUY else position.open_price - self.ask
        return move * position.volume * self.contract_size

    def _trigger_pending(self):
        for ticket, order in list(self._pending.items()):
            buy = order.order_type.value == "BUY_STOP"
            if not ((buy and self.ask >= order.price) or (not buy and self.bid <= order.price)):
                continue
            self.counter += 1
            key = str(self.counter)
            self._positions[key] = Position(
                key, order.symbol, OrderSide.BUY if buy else OrderSide.SELL, order.volume,
                self.ask if buy else self.bid, 0, 0, self.when.isoformat(), 0,
                self._pending_magic.pop(ticket))
            self._pending.pop(ticket)

    def close_position(self, ticket):
        result = super().close_position(ticket)
        return result

    def get_settlement_time(self, ticket):
        return self.when.to_pydatetime()


def replay(frame, config, balance=1000.0, contract_size=100.0, model=None):
    frame = frame.copy()
    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    if frame.empty or not frame["time"].is_monotonic_increasing:
        raise ValueError("Ticks must be nonempty and chronological")
    if not np.isfinite(frame[["bid", "ask"]].to_numpy()).all() or (frame.bid <= 0).any() or (frame.ask < frame.bid).any():
        raise ValueError("Invalid Bid/Ask ticks")
    previous_engine, previous_session = db.engine, db.SessionLocal
    with tempfile.TemporaryDirectory() as folder:
        isolated = create_engine("sqlite:///" + str(Path(folder) / "replay.db"))
        db.engine, db.SessionLocal = isolated, sessionmaker(bind=isolated, expire_on_commit=False)
        try:
            db.init_db()
            broker = ReplayBroker(config.get("symbol", "XAUUSDm"), balance, contract_size)
            broker.connect()
            engine = GridEngine(broker=broker, mode="demo", **config)
            if model:
                from app.ai.gate import AIGate
                engine.ai_gate = AIGate(model, "filter")
            next_poll = frame.time.iloc[0]
            peak, worst, max_open = balance, 0.0, 0
            curve = []
            for row in frame.itertuples(index=False):
                broker.step(row)
                if row.time >= next_poll:
                    engine._tick()
                    next_poll = row.time + pd.Timedelta(seconds=engine.poll_interval_seconds)
                equity = broker.get_account_info().equity
                peak = max(peak, equity)
                worst = max(worst, (peak-equity)/peak*100)
                max_open = max(max_open, len(broker._positions))
                curve.append(equity)
            results = list(broker._closed_profits.values())
            gains = sum(v for v in results if v > 0)
            losses = -sum(v for v in results if v < 0)
            return {
                "ending_balance": broker._balance, "ending_equity": curve[-1],
                "net_including_floating": curve[-1]-balance,
                "closed_positions": len(results), "remaining_positions": len(broker._positions),
                "remaining_orders": len(broker._pending), "max_positions": max_open,
                "profit_factor_closed_positions": gains/losses if losses else None,
                "max_drawdown_percent": worst, "last_error": engine._last_error,
                "limitations": "Historical replay with actual spread and quote gaps. No broker rejection, latency, margin stop-out, commission or swap simulation. Not proof of live profitability.",
            }
        finally:
            isolated.dispose()
            db.engine, db.SessionLocal = previous_engine, previous_session


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv")
    parser.add_argument("--settings", default="runtime_settings.json")
    parser.add_argument("--balance", type=float, required=True)
    parser.add_argument("--contract-size", type=float, default=100)
    parser.add_argument("--model")
    args = parser.parse_args()
    saved = json.loads(Path(args.settings).read_text())
    mapping = {"symbol": "symbol", "poll_interval_seconds": "poll_interval_seconds", "timezone": "timezone_name"}
    for key in ("lot_size", "buy_stop_levels", "sell_stop_levels", "grid_distance",
                "basket_take_profit_usd", "basket_stop_loss_usd", "max_open_positions",
                "max_daily_loss_usd", "max_equity_drawdown_percent", "magic_number",
                "trading_start_hour", "trading_end_hour", "daily_profit_target_usd"):
        mapping["grid_" + key if key != "grid_distance" else key] = key
    config = {target: saved[source] for source, target in mapping.items() if source in saved}
    print(json.dumps(replay(pd.read_csv(args.csv), config, args.balance, args.contract_size, args.model), indent=2))


if __name__ == "__main__":
    main()
