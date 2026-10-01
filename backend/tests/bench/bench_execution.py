"""Repeatable offline benchmark of the protective path.

This is a SYNTHETIC benchmark. Every broker call is a sleep of a configured
duration against an in-process double — there is no terminal, no network and no
MetaTrader. What it measures is how much of this process's own critical path is
spent waiting on work that a protective decision does not need. It says nothing
about real fill times, which require Windows verification.

The per-call delays below are deliberately modest and are stated, not derived
from the owner's account:

    account_info        5 ms
    positions          12 ms   (MT5 reads deal history per position)
    pendings            5 ms
    candles            25 ms
    symbol_info         3 ms
    realized_profit     8 ms   per settled ticket
    history sweep     120 ms   full account history

Run:  python3 -m tests.bench.bench_execution
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

os.environ.setdefault("BROKER_MODE", "mock")
os.environ.setdefault("SYMBOL", "XAUUSD")
os.environ.setdefault("ACCOUNT_TYPE", "demo")
os.environ.setdefault("DATABASE_URL", "sqlite:///" + tempfile.mktemp(suffix=".db"))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db as db_module  # noqa: E402
from app.engine.instrumentation import Recorder, monotonic_ms  # noqa: E402
from tests.conftest import MAGIC, FakeBroker  # noqa: E402

DELAYS_MS = {
    "get_account_info": 5.0,
    "get_open_positions": 12.0,
    "get_pending_orders": 5.0,
    "get_candles": 25.0,
    "get_symbol_info": 3.0,
    "get_realized_profit": 8.0,
    "history_records": 120.0,
}


class SlowBroker(FakeBroker):
    """FakeBroker with a stated cost on every call, and a call counter.

    The counter matters as much as the timing: a path that calls
    get_open_positions four times per tick is slow for a reason that shows up
    in the count before it shows up in the clock.
    """

    history_sync_interval = 30

    def __init__(self, *args, delays=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.delays = dict(DELAYS_MS if delays is None else delays)
        self.calls: dict[str, int] = {}
        self._pending_history: list[dict] = []

    def _cost(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1
        delay = self.delays.get(name, 0.0)
        if delay:
            time.sleep(delay / 1000.0)

    def get_account_info(self):
        self._cost("get_account_info")
        return super().get_account_info()

    def get_open_positions(self, symbol=None, magic=None):
        self._cost("get_open_positions")
        return super().get_open_positions(symbol, magic)

    def get_pending_orders(self, symbol=None, magic=None):
        self._cost("get_pending_orders")
        return super().get_pending_orders(symbol, magic)

    def get_candles(self, symbol, timeframe, count):
        self._cost("get_candles")
        return super().get_candles(symbol, timeframe, count)

    def get_symbol_info(self, symbol):
        self._cost("get_symbol_info")
        return super().get_symbol_info(symbol)

    def get_realized_profit(self, ticket):
        self._cost("get_realized_profit")
        return super().get_realized_profit(ticket)

    def history_records(self, symbol, magic):
        """Present so the engine takes its history-sync path, as MT5 does."""
        self._cost("history_records")
        return []

    def acknowledge_history(self):
        pass


def build_engine(broker, **kw):
    from app.engine.grid_engine import GridEngine

    kw.setdefault("magic_number", MAGIC)
    kw.setdefault("capital_floor_usd", 50.0)
    kw.setdefault("basket_stop_loss_usd", 60.0)
    kw.setdefault("max_daily_loss_usd", 10_000.0)
    return GridEngine(broker=broker, symbol="XAUUSD", mode="demo", **kw)


def measure(label: str, iterations: int = 40, protective_only: bool = False) -> dict:
    db_module.init_db()
    broker = SlowBroker()
    engine = build_engine(broker)
    engine._tick()
    broker.next_candle()
    engine._tick()          # grid armed
    broker.price += 4.0     # fill the buy side so there is exposure to value

    recorder = Recorder()
    broker.calls.clear()
    for _ in range(iterations):
        broker.next_candle()
        started = monotonic_ms()
        if protective_only and hasattr(engine, "_protective_tick"):
            engine._protective_tick()
            name = "protective_tick"
        else:
            engine._tick()
            name = "full_tick"
        recorder.record(name, monotonic_ms() - started)

    report = recorder.report()
    only = next(iter(report.values()))
    return {
        "label": label,
        "timing": only,
        "broker_calls_per_iteration": {
            k: round(v / iterations, 2) for k, v in sorted(broker.calls.items())
        },
    }


def main() -> None:
    print("SYNTHETIC benchmark — in-process doubles, no terminal involved.")
    print(f"stated per-call delays (ms): {DELAYS_MS}\n")

    full = measure("current full tick", iterations=40)
    print(f"--- {full['label']}")
    t = full["timing"]
    print(f"    n={t['count']}  median={t['median_ms']} ms  max={t['max_ms']} ms")
    print(f"    broker calls/iteration: {full['broker_calls_per_iteration']}")

    from app.engine.grid_engine import GridEngine

    if hasattr(GridEngine, "_protective_tick"):
        prot = measure("protective tick only", iterations=40, protective_only=True)
        print(f"\n--- {prot['label']}")
        t2 = prot["timing"]
        print(f"    n={t2['count']}  median={t2['median_ms']} ms  max={t2['max_ms']} ms")
        print(f"    broker calls/iteration: {prot['broker_calls_per_iteration']}")
        if t["median_ms"]:
            saved = t["median_ms"] - t2["median_ms"]
            print(f"\n    median protective-path reduction: {saved:.1f} ms "
                  f"({saved / t['median_ms'] * 100:.0f}%)")
    else:
        print("\n(no _protective_tick yet — this is the baseline)")


if __name__ == "__main__":
    main()
