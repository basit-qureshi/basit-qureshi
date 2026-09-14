"""Read-only MT5 tick export; run on Windows with the terminal connected."""
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pandas as pd
from app.brokers import get_broker


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--symbol", default="XAUUSDm")
    parser.add_argument("--output", default="data/ticks.csv")
    args = parser.parse_args()
    if not 1 <= args.days <= 90:
        raise ValueError("Days must be between 1 and 90")
    broker = get_broker()
    broker.connect()
    target = Path(args.output)
    temporary = target.with_suffix(".partial.csv")
    written = 0
    try:
        if not hasattr(broker, "_mt5"):
            raise ValueError("BROKER_MODE=mt5 is required")
        broker.get_symbol_info(args.symbol)
        target.parent.mkdir(parents=True, exist_ok=True)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=args.days)
        while start < end:
            until = min(start + timedelta(days=1), end)
            raw = broker._mt5.copy_ticks_range(args.symbol, start, until, broker._mt5.COPY_TICKS_ALL)
            if raw is None:
                raise RuntimeError(str(broker._mt5.last_error()))
            frame = pd.DataFrame(raw)
            if len(frame):
                frame["time"] = pd.to_datetime(frame.time_msc, unit="ms", utc=True)
                frame = frame[(frame.time >= start) & (frame.time < until) & (frame.bid > 0) & (frame.ask >= frame.bid)]
                if len(frame):
                    frame[["time", "bid", "ask"]].to_csv(
                        temporary, index=False, mode="a" if written else "w", header=not written)
                    written += len(frame)
            start = until
        if not written:
            raise RuntimeError("No usable ticks exported; existing output was not replaced")
        temporary.replace(target)
        print(f"Exported {written} ticks to {target}; no orders sent")
    finally:
        temporary.unlink(missing_ok=True)
        broker.disconnect()


if __name__ == "__main__":
    main()
