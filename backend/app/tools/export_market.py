"""Export local broker data. This script never sends an order."""
import argparse
import json
from pathlib import Path
from app.brokers import get_broker


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="XAUUSDm")
    parser.add_argument("--candles", type=int, default=40000)
    parser.add_argument("--output", default="data/candles.csv")
    args = parser.parse_args()
    broker = get_broker()
    broker.connect()
    try:
        account = broker.get_account_info()
        df = broker.get_candles(args.symbol, "M1", args.candles).iloc[:-1]
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(target, index_label="time")
        print(json.dumps({"file": str(target), "rows": len(df), "mode": account.trade_mode,
                          "symbol": args.symbol, "note": "Closed M1 candles; no orders sent"}))
    finally:
        broker.disconnect()


if __name__ == "__main__":
    main()
