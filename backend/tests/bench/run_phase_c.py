"""Phase C research harness — manifest, candidates, and what is missing.

IMPORTANT. This run uses a SYNTHETIC tick fixture because no real market data
exists in this repository. It therefore demonstrates that the machinery is
causal and produces the right shape of answer. It establishes NOTHING about
whether any candidate is profitable. Strategy selection is BLOCKED BY DATA.

Run:  python3 -m tests.bench.run_phase_c
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ.setdefault("BROKER_MODE", "mock")
os.environ.setdefault("SYMBOL", "XAUUSD")
os.environ.setdefault("ACCOUNT_TYPE", "demo")
os.environ.setdefault("DATABASE_URL", "sqlite:///" + tempfile.mktemp(suffix=".db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.research.causal_replay import ReplayCosts, StraddleReplay  # noqa: E402
from app.research.experiment_log import ExperimentLog  # noqa: E402
from app.research.splits import chronological_split  # noqa: E402
from app.research.tick_data import REQUIRED_EXPORT, build_manifest  # noqa: E402
from app.strategy import profiles as P  # noqa: E402
from app.strategy.admission import evaluate_all  # noqa: E402
from app.strategy.trailing import BasketTrailing, TrailingState  # noqa: E402

UTC = timezone.utc
START = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)


class Tick:
    __slots__ = ("time", "bid", "ask")

    def __init__(self, time, bid, ask):
        self.time, self.bid, self.ask = time, bid, ask

    @property
    def mid(self):
        return (self.bid + self.ask) / 2.0


def synthetic_ticks(n=6000, half=0.12):
    """A deterministic path with trending and ranging stretches.

    It is a FIXTURE, not a market. Its only job is to exercise both of the
    strategy's regimes: a directional run it can profit from, and a chop it
    freezes in.
    """
    ticks = []
    price = 4000.0
    for i in range(n):
        block = i // 500
        if block % 2 == 0:
            # A directional stretch long enough for the straddle to reach its
            # target: ~5 price units over 500 ticks, alternating direction so
            # neither side is favoured over the run.
            price += 0.010 if (block // 2) % 2 == 0 else -0.010
        else:
            # A range the straddle freezes in: it oscillates far enough to fill
            # BOTH sides and then returns, which is the known failure mode.
            price += 0.10 * (math.sin(i / 9.0) - math.sin((i - 1) / 9.0))
        spread = half * (3.0 if (i % 900) < 25 else 1.0)   # periodic widening
        ticks.append(Tick(START + timedelta(seconds=i), price - spread, price + spread))
    return ticks


def closed_bars_up_to(ticks, index, seconds=60, count=30):
    """Bars built only from ticks strictly BEFORE `index`, and only from bars
    that have fully closed. The bar currently forming is excluded entirely."""
    if index == 0:
        return [], None
    window = ticks[:index]
    bars, bucket, bucket_start = [], [], window[0].time
    for tick in window:
        if (tick.time - bucket_start).total_seconds() >= seconds:
            if bucket:
                bars.append({"high": max(t.ask for t in bucket),
                             "low": min(t.bid for t in bucket),
                             "close": bucket[-1].mid,
                             "close_time": bucket_start + timedelta(seconds=seconds)})
            bucket, bucket_start = [], tick.time
        bucket.append(tick)
    # `bucket` is the FORMING bar. It is deliberately discarded.
    return bars[-count:], (bars[-1]["close_time"] if bars else None)


def admission_for(profile, ticks):
    def fn(tick, index):
        bars, bar_as_of = closed_bars_up_to(ticks, index)
        ctx = {
            "now": tick.time,
            "bid": tick.bid, "ask": tick.ask,
            "quote_as_of": tick.time, "quote_age_ms": 0.0,
            "grid_distance": 0.30,
            "closed_bars": bars, "bar_as_of": bar_as_of,
            "calendar": None,          # none exists — the event gate must block
        }
        return evaluate_all(profile.build_gates(), ctx, profile.name, profile.version,
                            unknown_blocks=profile.unknown_blocks)
    return fn


def replay():
    return StraddleReplay(lot=0.01, buy_levels=10, sell_levels=10, spacing=0.30,
                          target_usd=10.0, stop_usd=60.0,
                          costs=ReplayCosts(commission_per_lot_per_side=2.75))


def trailing_factory_for(profile):
    if not profile.trailing:
        return None
    profile.trailing.validate_against_target(10.0)

    def make(basket_id):
        return BasketTrailing(profile.trailing, TrailingState(basket_id, "research"))
    return make


def main():
    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                            text=True, cwd="..").stdout.strip() or "unknown"

    print("=" * 78)
    print("PHASE C RESEARCH HARNESS")
    print("=" * 78)

    manifest = build_manifest(
        source_commit=commit,
        config={"lot": 0.01, "buy_levels": 10, "sell_levels": 10, "spacing": 0.30,
                "basket_target_usd": 10.0, "basket_stop_usd": 60.0},
        symbol_spec={"symbol": "XAUUSD", "point": 0.01, "value_per_point_per_lot": 1.0,
                     "note": "production symbol may carry a broker suffix, e.g. XAUUSDm"},
        dataset_path=None, dataset_sha256=None, quality=None,
        costs={"commission_per_lot_per_side": 2.75, "swap": "not modelled", "slippage": 0.0},
        fill_model={"basis": "observed bid/ask ticks",
                    "gapped_fills": "priced at the observed tick, never at the crossed level"},
        calendar_report=None,
    )
    print("\n--- BASELINE MANIFEST (extract) ---")
    print(json.dumps({k: manifest[k] for k in ("source_commit", "dataset", "calendar", "costs")},
                     indent=2))

    print("\n" + "!" * 78)
    print("! NO REAL MARKET DATA IS PRESENT.  dataset.present = False")
    print("! Everything below runs on a SYNTHETIC fixture and demonstrates")
    print("! MECHANICS ONLY. Strategy selection is BLOCKED BY DATA.")
    print("!" * 78)
    print(REQUIRED_EXPORT)

    ticks = synthetic_ticks()
    plan = chronological_split(ticks[0].time, ticks[-1].time,
                               development_fraction=0.5, validation_folds=3)
    print("--- CHRONOLOGICAL SPLIT (time-based, with embargo) ---")
    print(json.dumps(plan.as_dict(), indent=2))

    log = ExperimentLog(budget=6)     # declared BEFORE any result is looked at
    costs = replay().costs
    print("\n--- COST ASSUMPTIONS, identical for every profile below ---")
    print(f"  commission      : {costs.commission_per_lot_per_side} per lot per side "
          f"(charged on entry and exit, already inside every figure)")
    print(f"  swap            : {costs.swap_per_lot_per_day} per lot per day "
          f"({'NOT MODELLED' if not costs.swap_per_lot_per_day else 'modelled'})")
    print(f"  slippage        : {costs.slippage} price units beyond the quoted side")
    print(f"  decision latency: {costs.decision_latency}")
    print("  spread          : paid through the quoted side — a fill takes ask for a")
    print("                    BUY and bid for a SELL, and marking closes at the")
    print("                    other side, so entry AND exit spread are included")

    print("\n--- CANDIDATES on the synthetic fixture (MECHANICS ONLY) ---")
    # Realised and open are printed as separate columns, and then added, because
    # a profile that completes no basket and leaves one open at -24.58 is not a
    # flat 0.00 result — and a realised figure alone says exactly that.
    header = (f"{'profile':<30}{'done':>5}{'realised':>10}{'open':>5}"
              f"{'open mkd':>10}{'TOTAL mkd':>11}{'comm':>8}{'blocked':>8}{'worst':>9}")
    print(header)
    print("-" * len(header))

    for profile in (P.BASELINE, P.EXECUTION_QUALITY, P.REGIME, P.EVENT_BLACKOUT,
                    P.TRAILING, P.EXECUTION_AND_REGIME):
        fn = admission_for(profile, ticks) if profile.gate_factories else None
        report = replay().run(ticks, profile=profile, admission_fn=fn,
                              trailing_factory=trailing_factory_for(profile),
                              warmup_ticks=300)
        s = report.summary()
        log.record(profile.key, profile.as_dict().get("trailing") or {}, "synthetic", s)
        # `net_result` sums CLOSED baskets only; `remaining_exposure_marked` is
        # what is still open. The two sets are disjoint, so adding them is the
        # whole picture and double-counts nothing.
        total_marked = round(s["net_result"] + s["remaining_exposure_marked"], 2)
        print(f"{profile.name:<30}{s['baskets_completed']:>5}{s['net_result']:>10.2f}"
              f"{s['baskets_still_open']:>5}{s['remaining_exposure_marked']:>10.2f}"
              f"{total_marked:>11.2f}{s['commission_paid']:>8.2f}"
              f"{s['admissions_blocked']:>8}{s['worst_basket_marked']:>9.2f}")

    print("\n  Column definitions, because 'net' on its own is ambiguous:")
    print("    done      = baskets that reached an exit")
    print("    realised  = sum over CLOSED baskets only, commission included")
    print("                (this is the field the summary calls `net_result`)")
    print("    open      = baskets still open at the end of the fixture")
    print("    open mkd  = those baskets marked at the last observed tick,")
    print("                commission included, never dropped")
    print("    TOTAL mkd = realised + open mkd. The two sets are disjoint;")
    print("                nothing is counted twice")
    print("    worst     = worst marked value any basket passed through")
    print("\n  A profile with done=0 and realised=0.00 has NOT broken even.")
    print("  Compare profiles on TOTAL mkd, never on realised alone.")
    print("  One basket is one observation. These counts cannot support a")
    print("  selection, and a smaller loss on one synthetic fixture is not an edge.")

    print("\n--- EXPERIMENT LOG ---")
    state = log.as_dict()
    print(f"declared budget      : {state['declared_budget']}")
    print(f"candidates tried     : {len(state['distinct_candidates_tried'])}")
    print(f"over budget          : {state['over_budget']}")
    print(f"frozen selection     : {state['frozen']}")
    print(f"final window opened  : {state['final_opened_for']}")
    print("\nNothing was frozen and the final window was NOT opened: a synthetic")
    print("fixture cannot support a selection. That step needs real tick data.")

    print("\n" + "=" * 78)
    print("SELECTION RESULT: INSUFFICIENT EVIDENCE — BLOCKED BY DATA")
    print("=" * 78)


if __name__ == "__main__":
    main()
