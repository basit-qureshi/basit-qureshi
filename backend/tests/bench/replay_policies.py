"""Old versus new protective cadence over identical tick fixtures.

Repeatable and deterministic. Every scenario runs both policies against the
same tick list, the same starting positions and the same cost model; the only
difference is how often the protective decision is allowed to run.

This is a MECHANICS comparison. It is not a backtest, the fills are simulated
under a stated model, and none of it is evidence of profitability.

Run:  python3 -m tests.bench.replay_policies
"""

from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("BROKER_MODE", "mock")
os.environ.setdefault("SYMBOL", "XAUUSD")
os.environ.setdefault("DATABASE_URL", "sqlite:///" + tempfile.mktemp(suffix=".db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest.execution_replay import (  # noqa: E402
    CostModel, SimPosition, Tick, compare_exit_only,
)

STOP = 15.0
OLD_EVERY = 20      # one combined cycle, behind reporting work
NEW_EVERY = 2       # protection on its own cadence
METRICS = [
    "net_result", "worst_marked", "limit_overshoot",
    "ticks_to_confirmed_flat", "close_requests", "close_failures",
    "positions_left_open",
]


def longs():
    return [SimPosition("BUY", 0.01, 4000.0) for _ in range(10)]


def descending(n=160, step=0.08, half=0.12, unseen=0, widen_from=None, dead=()):
    ticks = []
    for i in range(n):
        mid = 4000.0 - i * step
        spread_half = 0.60 if (widen_from is not None and i >= widen_from) else half
        ticks.append(Tick(
            index=i, bid=mid - spread_half, ask=mid + spread_half,
            observable=not (unseen and i % unseen == 0),
            disconnected=any(lo <= i <= hi for lo, hi in dead),
        ))
    return ticks


def reversal(n=160):
    ticks = []
    for i in range(n):
        mid = 4000.0 - i * 0.30 if i < 30 else 4000.0 - 9.0 + (i - 30) * 0.25
        ticks.append(Tick(i, mid - 0.12, mid + 0.12))
    return ticks


def show(name, ticks, costs=None, note=None):
    out = compare_exit_only(ticks, longs, STOP,
                            old_decision_every=OLD_EVERY, new_decision_every=NEW_EVERY,
                            costs=costs)
    old, new = out["old"], out["new"]
    print(f"\n### {name}")
    if note:
        print(f"    ({note})")
    print(f"    {'metric':<26}{'old':>14}{'new':>14}")
    for key in METRICS:
        o, n = old[key], new[key]
        fo = f"{o:.2f}" if isinstance(o, float) else str(o)
        fn = f"{n:.2f}" if isinstance(n, float) else str(n)
        print(f"    {key:<26}{fo:>14}{fn:>14}")
    if old["positions_left_open"]:
        print(f"    !! the OLD policy never reached a confirmed flat state — "
              f"{old['positions_left_open']} position(s) still open at "
              f"{old['worst_marked']:.2f} marked. Its net_result of "
              f"{old['net_result']:.2f} is unrealised, not a better outcome.")
    return out


def main():
    print("EXIT-ONLY comparison — identical positions, ticks and cost model.")
    print(f"old = decide every {OLD_EVERY} ticks; new = decide every {NEW_EVERY}.")
    print("Simulated fills under a stated model. NOT evidence of profitability.")

    show("steady adverse move", descending())
    show("adverse move then reversal", reversal(),
         note="the case a latched intent exists for")
    show("spread widens before the decision", descending(step=0.03, widen_from=10),
         note="widening arrives while the basket is still open")
    show("terminal unreachable across the decision window",
         descending(step=0.03, dead=((15, 45),)),
         note="neither policy can act while disconnected; both resume after")
    show("rejected closes and partial fills", descending(),
         costs=CostModel(reject_first_n=3, partial_fill_every=2))
    show("1 tick in 4 is never observed", descending(unseen=4),
         note="the old cadence aliases with the gap and never lands on a visible tick")

    print("\nCaveats that apply to every row above:")
    for line in (
        "A sampled tick list is a sample. Anything between two samples did not happen here.",
        "Candle OHLC cannot order intrabar events, so no path is inferred from a bar.",
        "These fixtures validate mechanics. They do not establish a trading edge.",
        "Real fills, real spread and real latency require Windows verification.",
    ):
        print(f"  - {line}")


if __name__ == "__main__":
    main()
