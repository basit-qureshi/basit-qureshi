"""Local trained softmax classifier. Model probabilities are not profit guarantees."""
import json
from pathlib import Path
import numpy as np
import pandas as pd

FEATURES = ["r1", "r5", "r15", "vol", "range", "trend"]


def features(df):
    close = df["close"].astype(float)
    out = pd.DataFrame(index=df.index)
    out["r1"] = close.pct_change()
    out["r5"] = close.pct_change(5)
    out["r15"] = close.pct_change(15)
    out["vol"] = close.pct_change().rolling(30).std()
    out["range"] = (df["high"] - df["low"]) / close
    out["trend"] = (close.ewm(span=9, adjust=False).mean() - close.ewm(span=21, adjust=False).mean()) / close
    return out.replace([np.inf, -np.inf], np.nan)


def probabilities(x, model):
    z = (np.asarray(x) - np.asarray(model["mean"])) / np.asarray(model["scale"])
    z = np.clip(z, -10, 10)
    logits = np.c_[np.ones(len(z)), z] @ np.asarray(model["weights"])
    logits -= logits.max(axis=1, keepdims=True)
    exp = np.exp(logits)
    return exp / exp.sum(axis=1, keepdims=True)


class AIGate:
    def __init__(self, path, mode="shadow", confidence=0.6):
        if mode not in ("off", "shadow", "filter"):
            raise ValueError("AI_MODE must be off, shadow or filter")
        self.path = Path(path)
        self.mode = mode
        self.confidence = confidence
        self.reason = "Model has not been evaluated yet"
        self.direction = "BOTH"
        self.probability = None

    def status(self):
        return {"mode": self.mode, "reason": self.reason, "direction": self.direction,
                "probability": self.probability, "model_available": self.path.exists()}

    def allow(self, broker, symbol):
        self.direction = "BOTH"
        self.probability = None
        if self.mode == "off":
            self.reason = "AI disabled"
            return True
        try:
            model = json.loads(self.path.read_text())
            if model["version"] != 1 or model["symbol"] != symbol or model["features"] != FEATURES:
                raise ValueError("Model symbol or feature schema mismatch")
            data = broker.get_candles(symbol, "M1", 256).iloc[:-1]  # closed candles only
            if len(data) < 60:
                raise ValueError("Insufficient closed candles")
            stamp = pd.Timestamp(data.index[-1])
            stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
            trained = pd.Timestamp(model["training_end"])
            if stamp <= trained:
                raise ValueError("Model contains future data relative to this decision")
            if stamp - trained > pd.Timedelta(days=30):
                raise ValueError("Model is older than 30 days")
            x = features(data).iloc[-1:][FEATURES].to_numpy()
            if not np.isfinite(x).all():
                raise ValueError("Features contain invalid values")
            prob = probabilities(x, model)[0]
            if not np.isfinite(prob).all():
                raise ValueError("Invalid model output")
            label = int(prob.argmax())
            self.probability = round(float(prob[label]), 4)
            decision = ["SELL", "HOLD", "BUY"][label]
            eligible = bool(model["evaluation"]["research_eligible"])
            self.reason = f"{decision}, probability {self.probability}; research eligible: {eligible}"
            if self.mode == "filter":
                self.direction = decision
                return eligible and label != 1 and self.probability >= self.confidence
            return True
        except Exception as exc:
            self.reason = "AI unavailable: " + str(exc)
            return self.mode == "shadow"
