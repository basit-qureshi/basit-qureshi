"""A small regularized linear model, stored as JSON.

Why not pickle or joblib. Loading a pickle executes whatever the file says to
execute. A model artifact is data that arrives from somewhere, and "somewhere"
is exactly the wrong place to take code from. This model serializes to plain
JSON — coefficients, intercept, scaling statistics, schema — and scoring is a
dot product implemented here. There is no code path in this repository that
deserializes an arbitrary model file, which is a stronger guarantee than
checking one.

The model is deliberately small: ridge regression fitted with a closed-form
solve on standardized features. It runs on CPU in milliseconds, needs no GPU,
and its coefficients are readable, which matters more than capacity for a
dataset that does not exist yet.

Two heads are fitted, not one: expected **net** and expected **downside**.
Training a single win/lose classifier would answer "how often" when the
question is "how much, and how badly when it goes wrong".
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    import numpy as np
except ImportError:                      # pragma: no cover - numpy is a dependency
    np = None

MODEL_FORMAT = "goldgrid-linear-v1"


class ModelLoadError(RuntimeError):
    """An artifact could not be trusted. Never downgraded to a warning."""


@dataclass
class ModelManifest:
    """Everything needed to decide whether this artifact may be used at all."""

    model_version: str
    format: str
    feature_names: tuple[str, ...]
    feature_schema_version: str
    feature_fingerprint: str
    symbol: str
    profile_key: str
    trained_from: str | None
    trained_to: str | None
    source_commit: str
    dependency_versions: dict
    metrics: dict
    #: Approval is an owner decision recorded here. "trained" is not "approved",
    #: and the predictor refuses to run a model that is not approved for the
    #: mode it is being asked to operate in.
    approval_state: str = "unapproved"
    expires_at: str | None = None
    checksum: str | None = None
    notes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "model_version": self.model_version,
            "format": self.format,
            "feature_names": list(self.feature_names),
            "feature_schema_version": self.feature_schema_version,
            "feature_fingerprint": self.feature_fingerprint,
            "symbol": self.symbol,
            "profile_key": self.profile_key,
            "trained_from": self.trained_from,
            "trained_to": self.trained_to,
            "source_commit": self.source_commit,
            "dependency_versions": self.dependency_versions,
            "metrics": self.metrics,
            "approval_state": self.approval_state,
            "expires_at": self.expires_at,
            "checksum": self.checksum,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ModelManifest":
        known = set(cls.__dataclass_fields__)
        payload = {k: v for k, v in data.items() if k in known}
        payload["feature_names"] = tuple(payload.get("feature_names", ()))
        return cls(**payload)

    def expired(self, now: datetime | None = None) -> bool:
        if not self.expires_at:
            return False
        now = now or datetime.now(timezone.utc)
        try:
            return now > datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError:
            # An unparseable expiry is treated as expired. A date nobody can
            # read is not evidence the model is still current.
            return True


@dataclass
class LinearEntryModel:
    """Two ridge heads over the same standardized features."""

    manifest: ModelManifest
    mean: list
    scale: list
    net_coef: list
    net_intercept: float
    downside_coef: list
    downside_intercept: float

    # -- scoring -----------------------------------------------------------
    def _standardize(self, row):
        out = []
        for value, mean, scale in zip(row, self.mean, self.scale):
            out.append((value - mean) / scale if scale else 0.0)
        return out

    def predict(self, row: list) -> tuple[float, float]:
        if len(row) != len(self.manifest.feature_names):
            raise ValueError(
                f"expected {len(self.manifest.feature_names)} features, got {len(row)}"
            )
        z = self._standardize(row)
        net = self.net_intercept + sum(c * v for c, v in zip(self.net_coef, z))
        downside = self.downside_intercept + sum(c * v for c, v in zip(self.downside_coef, z))
        return float(net), float(downside)

    # -- persistence -------------------------------------------------------
    def payload(self) -> dict:
        return {
            "format": MODEL_FORMAT,
            "manifest": self.manifest.as_dict(),
            "mean": list(self.mean),
            "scale": list(self.scale),
            "net_coef": list(self.net_coef),
            "net_intercept": self.net_intercept,
            "downside_coef": list(self.downside_coef),
            "downside_intercept": self.downside_intercept,
        }

    @staticmethod
    def checksum_of(payload: dict) -> str:
        body = dict(payload)
        manifest = dict(body.get("manifest", {}))
        manifest.pop("checksum", None)
        body["manifest"] = manifest
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    def save(self, path: str | Path) -> Path:
        """Atomic write: a torn file is never left where a loader will find it."""
        path = Path(path)
        payload = self.payload()
        payload["manifest"]["checksum"] = self.checksum_of(payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)          # atomic on POSIX and Windows
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return path

    @classmethod
    def load(cls, path: str | Path) -> "LinearEntryModel":
        """Loads JSON only, and verifies identity before returning it."""
        path = Path(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ModelLoadError(f"{path} is not readable JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ModelLoadError(f"{path} does not contain a model object")
        if payload.get("format") != MODEL_FORMAT:
            raise ModelLoadError(
                f"{path} declares format {payload.get('format')!r}, expected {MODEL_FORMAT!r}"
            )
        manifest_data = payload.get("manifest")
        if not isinstance(manifest_data, dict):
            raise ModelLoadError(f"{path} has no manifest")

        declared = manifest_data.get("checksum")
        recomputed = cls.checksum_of(payload)
        if declared != recomputed:
            raise ModelLoadError(
                f"{path} failed its checksum: the artifact does not match its manifest"
            )

        manifest = ModelManifest.from_dict(manifest_data)
        try:
            model = cls(
                manifest=manifest,
                mean=list(payload["mean"]), scale=list(payload["scale"]),
                net_coef=list(payload["net_coef"]),
                net_intercept=float(payload["net_intercept"]),
                downside_coef=list(payload["downside_coef"]),
                downside_intercept=float(payload["downside_intercept"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelLoadError(f"{path} is missing or malformed: {exc}") from exc

        widths = {len(model.mean), len(model.scale), len(model.net_coef),
                  len(model.downside_coef), len(manifest.feature_names)}
        if len(widths) != 1:
            raise ModelLoadError(
                f"{path} has inconsistent widths across features and coefficients: {widths}"
            )
        return model


def fit_ridge(X, y, alpha: float = 1.0):
    """Closed-form ridge on standardized inputs. Returns (coef, intercept).

    Ridge rather than plain least squares because the features are correlated
    by construction (ATR appears in three of them), and a tiny dataset with
    correlated columns produces enormous unstable coefficients without it.
    """
    if np is None:
        raise RuntimeError("numpy is required to fit a model")
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    if X.ndim != 2 or X.shape[0] != y.shape[0]:
        raise ValueError(f"X {X.shape} and y {y.shape} do not line up")
    n_features = X.shape[1]
    # The intercept is not penalized: shrinking it toward zero would bias every
    # prediction toward a net result of zero for no statistical reason.
    intercept = float(y.mean())
    centred = y - intercept
    gram = X.T @ X + alpha * np.eye(n_features)
    coef = np.linalg.solve(gram, X.T @ centred)
    return coef.tolist(), intercept


def standardization(X):
    """Column means and scales, fitted ONLY on the window passed in.

    Fitting these on the whole dataset and then splitting is a classic leak:
    the training rows would carry information about the evaluation period's
    distribution.
    """
    if np is None:
        raise RuntimeError("numpy is required")
    X = np.asarray(X, dtype=float)
    mean = X.mean(axis=0)
    scale = X.std(axis=0)
    scale[scale == 0] = 1.0        # a constant column contributes nothing
    return mean.tolist(), scale.tolist()
