"""Train with a chronological holdout and a label-horizon purge. No broker orders."""
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
import pandas as pd
from app.ai.gate import FEATURES, features, probabilities


def train(df, symbol, cost=0.30, horizon=5):
    if len(df) < 3000 or not df.index.is_monotonic_increasing or df.index.has_duplicates:
        raise ValueError("At least 3000 ordered, unique M1 candles are required")
    if cost <= 0 or horizon < 1:
        raise ValueError("Positive transaction cost and horizon required")
    x = features(df)
    move = df["close"].shift(-horizon) - df["close"]
    valid = x.notna().all(axis=1) & move.notna()
    x, move = x.loc[valid], move.loc[valid]
    y = np.where(move > cost, 2, np.where(move < -cost, 0, 1))
    split = int(len(x) * 0.8)
    train_end = split - horizon
    a, b = x.iloc[:train_end].to_numpy(), x.iloc[split:].to_numpy()
    labels, test_y = y[:train_end], y[split:]
    if len(np.unique(labels)) < 3:
        raise ValueError("Training data must contain up, down and flat outcomes")
    mean, scale = a.mean(axis=0), np.maximum(a.std(axis=0), 1e-9)
    design = np.c_[np.ones(len(a)), np.clip((a - mean) / scale, -10, 10)]
    weights = np.zeros((len(FEATURES) + 1, 3))
    target = np.eye(3)[labels]
    for _ in range(400):
        logits = design @ weights
        logits -= logits.max(axis=1, keepdims=True)
        prob = np.exp(logits)
        prob /= prob.sum(axis=1, keepdims=True)
        penalty = weights.copy()
        penalty[0] = 0
        weights -= 0.05 * (design.T @ (prob - target) / len(a) + 0.001 * penalty)
    model = {"version": 1, "symbol": symbol, "features": FEATURES,
             "mean": mean.tolist(), "scale": scale.tolist(), "weights": weights.tolist(),
             "training_end": pd.Timestamp(x.index[train_end - 1]).isoformat(),
             "evaluation_end": pd.Timestamp(x.index[-1]).isoformat(),
             "horizon": horizon, "cost_price": cost}
    # Decisions may use holdout results for eligibility: model is only available
    # AFTER the holdout ends, preventing evaluation leakage during replay.
    model["training_end"] = model["evaluation_end"]
    pred = probabilities(b, model).argmax(axis=1)
    accuracy = float(np.mean(pred == test_y))
    baseline = float(np.mean(test_y == np.bincount(labels, minlength=3).argmax()))
    # Non-overlapping directional proxy only; this is NOT grid P&L.
    moves = move.iloc[split:].to_numpy()[::horizon]
    selected = pred[::horizon]
    proxy = np.where(selected == 2, moves - cost,
                     np.where(selected == 0, -moves - cost, 0))
    count = int(np.sum(selected != 1))
    model["evaluation"] = {
        "train_rows": len(a), "holdout_rows": len(b), "purge_rows": horizon,
        "accuracy": accuracy, "majority_baseline": baseline,
        "directional_proxy_price_sum": float(proxy.sum()), "proxy_decisions": count,
        "research_eligible": bool(accuracy > baseline and proxy.sum() > 0 and count >= 30),
        "warning": "Classifier holdout only. Broker tick replay and forward demo validation are still required.",
    }
    return model


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--cost", type=float, default=0.30)
    parser.add_argument("--output", default="models/model.json")
    args = parser.parse_args()
    raw = Path(args.csv).read_bytes()
    df = pd.read_csv(args.csv)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    model = train(df.set_index("time"), args.symbol, args.cost)
    model["source_sha256"] = hashlib.sha256(raw).hexdigest()
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(model, indent=2, allow_nan=False))
    print(json.dumps(model["evaluation"], indent=2))


if __name__ == "__main__":
    main()
