"""Train the entry model from a tick export. OFFLINE — no network, no broker.

    .\\venv\\Scripts\\python.exe tools\\train_entry_model.py \\
        --ticks data\\xauusdm_ticks.csv \\
        --out models\\entry-v1.json \\
        [--news data\\news.csv] [--calendar data\\calendar.csv]

It refuses to produce an artifact without real tick data. There is no
`--synthetic` flag that writes a model file: a model trained on a fixture that
is then loadable by the live system is exactly the artifact that eventually
gets trusted by accident.

Pipeline, in order, with the leak-prevention at each step:

1. Replay the BASELINE profile over the ticks to generate opportunities. The
   sampling rule is independent of any model — every admissible moment on a
   fixed cadence, not "the trades the old bot happened to take", which would
   train on a biased selection of history.
2. Build features at each opportunity from information available then.
3. Label outcomes by replaying the basket forward. Unresolved baskets are
   IMMATURE and excluded, never labelled zero.
4. Split chronologically with purge and embargo.
5. Fit standardization and ridge coefficients on the DEVELOPMENT window only.
6. Report validation metrics against a constant benchmark.
7. Write the artifact as JSON, unapproved, with an expiry.

The final evaluation window is NOT opened here. Freezing and opening it is a
separate, deliberate step.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ai.features import FEATURE_SCHEMA, FeatureInputs, build_features  # noqa: E402
from app.ai.labels import label_from_replay, summarize_labels  # noqa: E402
from app.ai.model import (  # noqa: E402
    LinearEntryModel, ModelManifest, fit_ridge, standardization,
)
from app.research.splits import chronological_split  # noqa: E402
from app.research.tick_data import REQUIRED_EXPORT, build_manifest, load_ticks_csv  # noqa: E402


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, cwd=Path(__file__).parent.parent.parent).stdout.strip()
    except Exception:
        return "unknown"


def dependency_versions() -> dict:
    versions = {"python": platform.python_version()}
    for name in ("numpy", "pandas", "sklearn"):
        try:
            module = __import__(name)
            versions[name] = getattr(module, "__version__", "?")
        except ImportError:
            versions[name] = "absent"
    return versions


def constant_benchmark(targets) -> dict:
    """Predicting the training mean for everything.

    Any model that cannot beat this has learned nothing, and reporting it
    alongside the model is what makes an R-squared meaningful.
    """
    if not targets:
        return {"mae": None, "n": 0}
    mean = sum(targets) / len(targets)
    return {"constant": round(mean, 4),
            "mae": round(sum(abs(t - mean) for t in targets) / len(targets), 4),
            "n": len(targets)}


def mean_absolute_error(predicted, actual) -> float | None:
    pairs = list(zip(predicted, actual))
    if not pairs:
        return None
    return round(sum(abs(p - a) for p, a in pairs) / len(pairs), 4)


def main() -> int:
    parser = argparse.ArgumentParser(description="Train the entry model offline.")
    parser.add_argument("--ticks", required=True, help="MT5 tick export CSV (bid/ask, UTC)")
    parser.add_argument("--out", required=True, help="destination model JSON")
    parser.add_argument("--calendar", help="optional calendar CSV")
    parser.add_argument("--news", help="optional news CSV")
    parser.add_argument("--alpha", type=float, default=1.0, help="ridge penalty")
    parser.add_argument("--horizon-minutes", type=int, default=240)
    parser.add_argument("--opportunity-every-ticks", type=int, default=600,
                        help="model-independent sampling cadence")
    parser.add_argument("--expires-days", type=int, default=90)
    args = parser.parse_args()

    tick_path = Path(args.ticks)
    if not tick_path.exists():
        print(f"ERROR: {tick_path} does not exist.\n", file=sys.stderr)
        print("No model can be trained without real tick history. The required "
              "export is:\n", file=sys.stderr)
        print(REQUIRED_EXPORT, file=sys.stderr)
        return 2

    started = time.monotonic()
    print(f"loading {tick_path} ...")
    ticks, quality, digest = load_ticks_csv(tick_path)
    if len(ticks) < 10_000:
        print(f"ERROR: only {len(ticks)} usable ticks. That is far too few to fit "
              f"and validate a model without overfitting it.", file=sys.stderr)
        return 3
    print(f"  {quality.rows_kept} ticks, {quality.first} .. {quality.last}")
    print(f"  gaps: {len(quality.gaps)}  duplicates: {quality.duplicate_timestamps}  "
          f"out of order: {quality.out_of_order}")

    calendar = None
    calendar_report = None
    if args.calendar:
        from app.research.calendar import load_calendar_csv
        calendar, calendar_report = load_calendar_csv(args.calendar)
        print(f"  calendar: {calendar_report['rows_loaded']} events")

    if args.news:
        from app.news.providers import LocalFileNewsProvider
        provider = LocalFileNewsProvider(args.news)
        _, news_report = provider.load()
        print(f"  news: {news_report.items_kept} items "
              f"({news_report.duplicates_collapsed} duplicates collapsed)")

    # --- opportunities, sampled independently of any model -----------------
    from app.research.causal_replay import ReplayCosts, StraddleReplay
    from app.strategy import profiles as P

    print(f"\nreplaying the baseline to generate opportunities "
          f"(every {args.opportunity_every_ticks} ticks) ...")
    replay = StraddleReplay(lot=0.01, buy_levels=10, sell_levels=10, spacing=0.30,
                            target_usd=10.0, stop_usd=60.0,
                            costs=ReplayCosts(commission_per_lot_per_side=2.75))
    horizon = timedelta(minutes=args.horizon_minutes)
    fill_assumptions = {"basis": "observed bid/ask ticks",
                        "gapped": "priced at the observed tick, never the crossed level",
                        "commission_per_lot_per_side": 2.75}

    rows, targets_net, targets_down, stamps = [], [], [], []
    opportunities = 0
    incomplete = 0
    for index in range(0, len(ticks) - 1, args.opportunity_every_ticks):
        tick = ticks[index]
        opportunities += 1
        bars, bar_as_of = _bars_before(ticks, index)
        features = build_features(FeatureInputs(
            decision_time=tick.time, bid=tick.bid, ask=tick.ask,
            quote_as_of=tick.time, grid_distance=0.30,
            closed_bars=bars, bar_as_of=bar_as_of,
        ))
        row = features.as_row()
        if row is None:
            incomplete += 1
            continue
        window = [t for t in ticks[index:] if t.time - tick.time <= horizon]
        if len(window) < 10:
            continue
        report = replay.run(window, profile=P.BASELINE)
        if not report.baskets:
            continue
        label = label_from_replay(report.baskets[0], horizon=horizon,
                                  fill_assumptions=fill_assumptions)
        if not label.trainable:
            continue
        rows.append(row)
        targets_net.append(label.net)
        targets_down.append(label.worst_marked)
        stamps.append(tick.time)

    print(f"  {opportunities} opportunities sampled, {incomplete} dropped for "
          f"incomplete features, {len(rows)} trainable")
    if len(rows) < 100:
        print(f"ERROR: only {len(rows)} trainable outcomes. Too few to fit and "
              f"validate honestly; nothing was written.", file=sys.stderr)
        return 4

    # --- chronological split ------------------------------------------------
    plan = chronological_split(stamps[0], stamps[-1], development_fraction=0.5,
                               validation_folds=3, embargo=timedelta(hours=6))
    dev = [i for i, s in enumerate(stamps) if plan.development.contains(s)]
    val = [i for i, s in enumerate(stamps)
           if any(w.contains(s) for w in plan.validation)]
    print(f"\nsplit: {len(dev)} development, {len(val)} validation, "
          f"final window reserved and NOT opened")
    if len(dev) < 50 or len(val) < 20:
        print("ERROR: a window is too small to fit or validate on.", file=sys.stderr)
        return 5

    # --- fit on DEVELOPMENT ONLY -------------------------------------------
    X_dev = [rows[i] for i in dev]
    mean, scale = standardization(X_dev)          # fitted inside the window
    Z_dev = [[(v - m) / s for v, m, s in zip(r, mean, scale)] for r in X_dev]
    net_coef, net_intercept = fit_ridge(Z_dev, [targets_net[i] for i in dev], args.alpha)
    down_coef, down_intercept = fit_ridge(Z_dev, [targets_down[i] for i in dev], args.alpha)

    # --- validation against a constant benchmark ---------------------------
    Z_val = [[(v - m) / s for v, m, s in zip(rows[i], mean, scale)] for i in val]
    predicted = [net_intercept + sum(c * v for c, v in zip(net_coef, z)) for z in Z_val]
    actual = [targets_net[i] for i in val]
    benchmark = constant_benchmark([targets_net[i] for i in dev])
    model_mae = mean_absolute_error(predicted, actual)
    print(f"\nvalidation MAE   : {model_mae}")
    print(f"constant benchmark: {benchmark['mae']} (predicting the training mean)")
    if model_mae is not None and benchmark["mae"] is not None and model_mae >= benchmark["mae"]:
        print("  the model does NOT beat the constant benchmark on validation.")

    metrics = {
        "validation_mae": model_mae,
        "constant_benchmark": benchmark,
        "beats_benchmark": bool(model_mae is not None and benchmark["mae"] is not None
                                and model_mae < benchmark["mae"]),
        "labels": summarize_labels([]),
        "n_development": len(dev), "n_validation": len(val),
        "final_window_opened": False,
    }

    manifest = ModelManifest(
        model_version=f"entry-{datetime.now(timezone.utc):%Y%m%d}-a{args.alpha}",
        format="goldgrid-linear-v1",
        feature_names=FEATURE_SCHEMA.names,
        feature_schema_version=FEATURE_SCHEMA.version,
        feature_fingerprint=FEATURE_SCHEMA.fingerprint,
        symbol="XAUUSD", profile_key="baseline@v1",
        trained_from=stamps[dev[0]].isoformat(), trained_to=stamps[dev[-1]].isoformat(),
        source_commit=git_commit(), dependency_versions=dependency_versions(),
        metrics=metrics,
        approval_state="unapproved",       # training is not approval
        expires_at=(datetime.now(timezone.utc) + timedelta(days=args.expires_days)).isoformat(),
        notes=["trained offline from a tick export; no broker was contacted",
               "outcomes are SIMULATED replay results, not observed broker profits",
               "the final evaluation window was not opened during training"],
    )
    model = LinearEntryModel(manifest=manifest, mean=mean, scale=scale,
                             net_coef=net_coef, net_intercept=net_intercept,
                             downside_coef=down_coef, downside_intercept=down_intercept)
    path = model.save(args.out)
    elapsed = time.monotonic() - started
    print(f"\nwrote {path} in {elapsed:.1f}s")
    print("approval_state = unapproved. The predictor refuses to use it until "
          "an owner approves it, and shadow evidence comes first.")

    data_manifest = build_manifest(
        source_commit=git_commit(),
        config={"lot": 0.01, "buy_levels": 10, "sell_levels": 10, "spacing": 0.30},
        symbol_spec={"symbol": "XAUUSD", "point": 0.01},
        dataset_path=str(tick_path), dataset_sha256=digest, quality=quality,
        costs={"commission_per_lot_per_side": 2.75},
        fill_model=fill_assumptions, calendar_report=calendar_report,
    )
    manifest_path = Path(args.out).with_suffix(".data-manifest.json")
    manifest_path.write_text(json.dumps(data_manifest, indent=2), encoding="utf-8")
    print(f"wrote {manifest_path}")
    return 0


def _bars_before(ticks, index, seconds=60, count=40):
    """Closed bars strictly before `index`. The forming bar is discarded."""
    if index == 0:
        return [], None
    window = ticks[max(0, index - 20000):index]
    bars, bucket, start = [], [], window[0].time
    for tick in window:
        if (tick.time - start).total_seconds() >= seconds:
            if bucket:
                bars.append({"high": max(t.ask for t in bucket),
                             "low": min(t.bid for t in bucket),
                             "close": bucket[-1].mid,
                             "close_time": start + timedelta(seconds=seconds)})
            bucket, start = [], tick.time
        bucket.append(tick)
    return bars[-count:], (bars[-1]["close_time"] if bars else None)


if __name__ == "__main__":
    raise SystemExit(main())
