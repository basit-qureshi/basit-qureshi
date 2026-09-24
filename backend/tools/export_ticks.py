"""Export MT5 tick history to CSV, with a coverage manifest. OWNER RUNS THIS.

This script reads history. It cannot trade: it imports nothing from `app`, so no
engine, no database and no order path is reachable from here, and the only MT5
calls it makes are `initialize`, `symbol_select`, `copy_ticks_range`, `version`
and `shutdown`. `MetaTrader5` is imported inside `main()`, so importing this
module touches no terminal at all. `tests/test_export_ticks.py` asserts all of
that offline, against a fake MT5 module.

It DOES need a logged-in MT5 terminal on Windows, because only the terminal has
the history. Nothing about risk limits applies here — a read-only export cannot
open a position, so it does not wait on any risk decision.

    .\\venv\\Scripts\\python.exe tools\\export_ticks.py --symbol XAUUSDm ^
        --from 2026-06-25 --to 2026-09-24 --out data\\xauusdm_ticks.csv

Both dates are INCLUSIVE and UTC. `--to 2026-09-24` includes everything up to
2026-09-24 23:59:59.999 UTC. The earlier version of this tool took `--to` as a
midnight boundary, so the last day came back empty; that trap is gone.

Why bid AND ask. This strategy pays the spread on up to twenty fills per basket
against a fixed cash target, so a mid-only export cannot price it. Rows without
a finite two-sided quote are skipped AND COUNTED, never silently dropped.

What the manifest is for. A successful request is not proof of coverage. The
manifest next to the CSV records what was asked for, what came back day by day,
which empty days are ordinary market closures and which are unexplained. Nothing
is ever filled in: a gap is reported as a gap.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

TOOL_VERSION = "export_ticks/2"
CSV_COLUMNS = ["time_utc", "bid", "ask", "last", "volume", "flags", "seq"]


def parse_day(text: str) -> date:
    return datetime.strptime(text, "%Y-%m-%d").date()


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """One UTC day, as the half-open interval the request uses."""
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def is_weekend(day: date) -> bool:
    """Saturday or Sunday in UTC.

    The FX/metals week closes late Friday and reopens Sunday evening UTC, so a
    Saturday with no ticks is expected and a Sunday usually has a partial
    evening session. This is a coarse rule and it is labelled as one: a public
    holiday looks like an unexplained gap here, and that is the safer error.
    """
    return day.weekday() >= 5


def existing_coverage(path: Path) -> tuple[date | None, int]:
    """Last exported day and row count, for --resume. Reads, never writes."""
    if not path.exists():
        return None, 0
    last_day, rows = None, 0
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            if header is None:
                return None, 0
            for row in reader:
                if not row:
                    continue
                rows += 1
                try:
                    last_day = datetime.fromisoformat(row[0]).date()
                except (ValueError, IndexError):
                    continue
    except OSError as exc:
        print(f"could not read {path} to resume: {exc}", file=sys.stderr)
        return None, 0
    return last_day, rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Export MT5 ticks (bid/ask, UTC) to CSV with a coverage manifest.")
    parser.add_argument("--symbol", required=True,
                        help="EXACT Market Watch name, suffix included, e.g. XAUUSDm")
    parser.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD (UTC, inclusive)")
    parser.add_argument("--to", dest="end", required=True, help="YYYY-MM-DD (UTC, inclusive)")
    parser.add_argument("--out", required=True, help="destination CSV path")
    parser.add_argument("--chunk-days", type=int, default=1,
                        help="days per request (default 1). One day at a time keeps "
                             "memory bounded and makes a partial export resumable")
    parser.add_argument("--resume", action="store_true",
                        help="append to an existing CSV, continuing after its last day")
    parser.add_argument("--overwrite", action="store_true",
                        help="replace an existing CSV instead of refusing")
    parser.add_argument("--manifest", default=None,
                        help="manifest path (default: <out>.manifest.json)")
    args = parser.parse_args(argv)

    if args.chunk_days < 1:
        print("--chunk-days must be at least 1", file=sys.stderr)
        return 2
    try:
        first_day, last_day = parse_day(args.start), parse_day(args.end)
    except ValueError as exc:
        print(f"bad date: {exc}", file=sys.stderr)
        return 2
    if last_day < first_day:
        print("--to is before --from", file=sys.stderr)
        return 2

    out = Path(args.out)
    manifest_path = Path(args.manifest) if args.manifest else Path(str(out) + ".manifest.json")
    resume_from, existing_rows = (None, 0)
    if out.exists():
        if args.resume:
            resume_from, existing_rows = existing_coverage(out)
            if resume_from is not None:
                # The last exported day is re-requested from scratch only if it
                # is the first day asked for; otherwise continue after it.
                first_day = max(first_day, resume_from + timedelta(days=1))
                print(f"resuming after {resume_from} ({existing_rows} rows already present)")
                if first_day > last_day:
                    print("the existing file already covers the requested range")
                    return 0
        elif not args.overwrite:
            print(f"{out} exists. Pass --resume to continue it or --overwrite to replace it.",
                  file=sys.stderr)
            return 2

    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("MetaTrader5 is not installed. Run this on the Windows machine "
              "inside the backend venv.", file=sys.stderr)
        return 3

    if not mt5.initialize():
        print(f"mt5.initialize() failed: {mt5.last_error()}", file=sys.stderr)
        return 4
    try:
        return _export(mt5, args, out, manifest_path, first_day, last_day, existing_rows)
    finally:
        mt5.shutdown()


def _export(mt5, args, out: Path, manifest_path: Path, first_day: date, last_day: date,
            existing_rows: int) -> int:
    if not mt5.symbol_select(args.symbol, True):
        print(f"could not select {args.symbol}: {mt5.last_error()}. Check the exact "
              f"Market Watch name including the broker's suffix.", file=sys.stderr)
        return 5

    days: list[dict] = []
    errors: list[str] = []
    written = skipped_nonfinite = out_of_order = 0
    previous_stamp: datetime | None = None
    seq = existing_rows
    mode = "a" if existing_rows else "w"

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open(mode, newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if mode == "w":
            writer.writerow(CSV_COLUMNS)

        day = first_day
        while day <= last_day:
            chunk_end = min(day + timedelta(days=args.chunk_days - 1), last_day)
            start, _ = day_bounds(day)
            _, end = day_bounds(chunk_end)
            try:
                ticks = mt5.copy_ticks_range(args.symbol, start, end, mt5.COPY_TICKS_INFO)
            except Exception as exc:                      # a terminal can drop mid-export
                errors.append(f"{day}..{chunk_end}: {exc}")
                ticks = None
            if ticks is None:
                errors.append(f"{day}..{chunk_end}: copy_ticks_range returned None "
                              f"({mt5.last_error()})")
                ticks = []

            rows_here = skipped_here = 0
            first_stamp = last_stamp = None
            for tick in ticks:
                msc = int(getattr(tick, "time_msc", 0) or 0)
                stamp = (datetime.fromtimestamp(msc / 1000.0, tz=timezone.utc) if msc
                         else datetime.fromtimestamp(int(tick.time), tz=timezone.utc))
                bid, ask = float(tick.bid), float(tick.ask)
                # NaN and inf both slip past a `<= 0` test, and a NaN quote in a
                # replay is worse than a missing row.
                if not (math.isfinite(bid) and math.isfinite(ask)) or bid <= 0 or ask <= 0:
                    skipped_here += 1
                    continue
                if previous_stamp is not None and stamp < previous_stamp:
                    out_of_order += 1
                previous_stamp = stamp
                seq += 1
                # `seq` is written because two distinct ticks can share a
                # millisecond. Both rows are kept, and the sequence tells them
                # apart for anything that would otherwise key on the timestamp.
                writer.writerow([stamp.isoformat(), bid, ask,
                                 float(getattr(tick, "last", 0.0) or 0.0),
                                 int(getattr(tick, "volume", 0) or 0),
                                 int(getattr(tick, "flags", 0) or 0), seq])
                rows_here += 1
                first_stamp = first_stamp or stamp
                last_stamp = stamp

            written += rows_here
            skipped_nonfinite += skipped_here
            for single in (day + timedelta(days=i) for i in range((chunk_end - day).days + 1)):
                days.append({
                    "day": single.isoformat(),
                    "weekday": single.strftime("%a"),
                    "rows": rows_here if single == day else None,
                    "rows_scope": f"{day}..{chunk_end}" if args.chunk_days > 1 else None,
                    "expected_closed": is_weekend(single),
                })
            if rows_here == 0 and not all(is_weekend(d) for d in
                                          (day + timedelta(days=i)
                                           for i in range((chunk_end - day).days + 1))):
                errors.append(f"{day}..{chunk_end}: no usable ticks on a day the market "
                              f"is normally open")
            print(f"  {day} .. {chunk_end}: {rows_here} rows"
                  + (f", {skipped_here} skipped" if skipped_here else "")
                  + (f", {first_stamp.isoformat()} .. {last_stamp.isoformat()}"
                     if first_stamp else ""))
            day = chunk_end + timedelta(days=1)

    manifest = {
        "tool": TOOL_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_requested": args.symbol,
        "terminal_version": str(getattr(mt5, "version", lambda: "unknown")()),
        "requested_range_utc": {"from": first_day.isoformat(), "to": last_day.isoformat(),
                                "inclusive": True},
        "rows_written_this_run": written,
        "rows_total_in_file": seq,
        "rows_skipped_no_finite_two_sided_quote": skipped_nonfinite,
        "rows_out_of_order": out_of_order,
        "days": days,
        "days_with_no_rows_expected_closed": sum(
            1 for d in days if d["expected_closed"] and not d["rows"]),
        "unexplained_gaps": [e for e in errors if "normally open" in e],
        "errors": errors,
        "coverage_is_complete": not errors and out_of_order == 0,
        "notes": [
            "Both requested dates are inclusive UTC days.",
            "A successful request is not proof of coverage: read days[] and errors[].",
            "Nothing was interpolated. An empty day is reported, never filled in.",
            "Weekend classification is coarse: a public holiday appears as an "
            "unexplained gap, which is the safer direction to be wrong in.",
            "seq distinguishes ticks that share a millisecond; both are kept.",
        ],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"\nwrote {written} rows this run ({seq} in the file) to {out}")
    print(f"manifest: {manifest_path}")
    if skipped_nonfinite:
        print(f"  {skipped_nonfinite} row(s) had no finite two-sided quote and were skipped")
    if out_of_order:
        print(f"  {out_of_order} row(s) arrived out of order — the file is not sorted")
    if manifest["unexplained_gaps"]:
        print(f"  {len(manifest['unexplained_gaps'])} unexplained empty day(s): "
              f"coverage is INCOMPLETE, see the manifest")
    if written == 0 and not existing_rows:
        print("no usable rows at all — this export cannot price anything.", file=sys.stderr)
        return 6
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
