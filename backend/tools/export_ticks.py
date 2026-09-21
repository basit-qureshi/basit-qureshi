"""Export MT5 tick history to CSV. FOR THE OWNER TO RUN ON WINDOWS.

This script is NOT run by this repository's tests and was NOT run during
development — no terminal was contacted. It reads history only and places no
orders, but it does require a logged-in MT5 terminal, so only you can run it.

    .\\venv\\Scripts\\python.exe tools\\export_ticks.py --symbol XAUUSDm \\
        --from 2026-06-01 --to 2026-09-20 --out data\\xauusdm_ticks.csv

Why bid AND ask. This strategy pays the spread on up to twenty fills per
basket against a fixed cash target. A mid-price export cannot price it, so the
exporter refuses to write one.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone


def main() -> int:
    parser = argparse.ArgumentParser(description="Export MT5 ticks (bid/ask, UTC) to CSV.")
    parser.add_argument("--symbol", required=True, help="exact Market Watch name, e.g. XAUUSDm")
    parser.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD (UTC)")
    parser.add_argument("--to", dest="end", required=True, help="YYYY-MM-DD (UTC)")
    parser.add_argument("--out", required=True, help="destination CSV path")
    args = parser.parse_args()

    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("MetaTrader5 is not installed. Run this on the Windows machine "
              "inside the backend venv.", file=sys.stderr)
        return 2

    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    if not mt5.initialize():
        print(f"mt5.initialize() failed: {mt5.last_error()}", file=sys.stderr)
        return 3
    try:
        if not mt5.symbol_select(args.symbol, True):
            print(f"could not select {args.symbol}: {mt5.last_error()}", file=sys.stderr)
            return 4
        # COPY_TICKS_INFO returns bid/ask changes, which is what this needs.
        ticks = mt5.copy_ticks_range(args.symbol, start, end, mt5.COPY_TICKS_INFO)
        if ticks is None or len(ticks) == 0:
            print(f"no ticks returned for {args.symbol} in that range: {mt5.last_error()}",
                  file=sys.stderr)
            return 5

        written = 0
        with open(args.out, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["time_utc", "bid", "ask", "last", "volume", "flags"])
            for tick in ticks:
                # MT5 tick times are UTC epoch seconds; time_msc is milliseconds.
                msc = int(getattr(tick, "time_msc", 0) or 0)
                stamp = (datetime.fromtimestamp(msc / 1000.0, tz=timezone.utc) if msc
                         else datetime.fromtimestamp(int(tick.time), tz=timezone.utc))
                bid, ask = float(tick.bid), float(tick.ask)
                if bid <= 0 or ask <= 0:
                    continue          # a row with no two-sided quote cannot price this strategy
                writer.writerow([stamp.isoformat(), bid, ask,
                                 float(getattr(tick, "last", 0.0) or 0.0),
                                 int(getattr(tick, "volume", 0) or 0),
                                 int(getattr(tick, "flags", 0) or 0)])
                written += 1
        print(f"wrote {written} ticks to {args.out} ({start.date()} .. {end.date()} UTC)")
        if written == 0:
            print("every row lacked a two-sided quote — this export is not usable.",
                  file=sys.stderr)
            return 6
        return 0
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
