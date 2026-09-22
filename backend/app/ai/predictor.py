"""Asking the model, safely — and never letting the answer matter too much.

Two invariants this module exists to hold:

**The protection loop never waits on a model.** Loading, scoring and every
failure mode here are bounded and happen off the protective path. A model that
is missing, corrupt, stale, incompatible or slow produces an abstention, not a
delay and not an exception that reaches the engine.

**A model cannot turn a block into an allow.** `decide` runs the deterministic
gates first. If they say no, the answer is no and the model is not even asked —
there is no argument the model can make, because it is never consulted. A test
asserts this against a model that returns an absurdly confident score.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from app.ai.contracts import (
    AbstainReason, AdmissionOutcome, AIMode, FeatureSchema, Prediction,
)
from app.ai.features import FEATURE_SCHEMA, FeatureVector
from app.ai.model import LinearEntryModel, ModelLoadError
from app.engine.instrumentation import monotonic_ms

logger = logging.getLogger("ai.predictor")


class EntryPredictor:
    """Wraps a loaded model with every refusal it is supposed to make."""

    def __init__(self, model: LinearEntryModel | None = None, *,
                 symbol: str, profile_key: str,
                 time_budget_ms: float = 50.0, mode: AIMode = AIMode.DISABLED,
                 required_approval: str = "approved"):
        self._lock = threading.Lock()
        self._model = model
        self.symbol = symbol
        self.profile_key = profile_key
        self.time_budget_ms = time_budget_ms
        self.mode = mode
        self.required_approval = required_approval
        self.abstentions: dict[str, int] = {}

    # -- lifecycle ---------------------------------------------------------

    @property
    def model(self) -> LinearEntryModel | None:
        with self._lock:
            return self._model

    def replace_model(self, model: LinearEntryModel | None) -> None:
        """Atomic swap under a lock, so a prediction never sees a half-model."""
        with self._lock:
            self._model = model

    @classmethod
    def from_path(cls, path, **kwargs) -> "EntryPredictor":
        """Loads if possible; a bad artifact yields a predictor that abstains.

        A load failure must not stop the bot from starting. The protective
        layer does not need a model and never did.
        """
        try:
            model = LinearEntryModel.load(path)
        except (ModelLoadError, FileNotFoundError, OSError) as exc:
            logger.warning("no usable model at %s: %s — the predictor will abstain", path, exc)
            model = None
        return cls(model, **kwargs)

    # -- prediction --------------------------------------------------------

    def _abstain(self, reason: AbstainReason, decision_time, codes=()) -> Prediction:
        self.abstentions[reason.value] = self.abstentions.get(reason.value, 0) + 1
        model = self._model
        return Prediction.abstain(
            reason, decision_time=decision_time,
            model_version=model.manifest.model_version if model else "none",
            profile_version=self.profile_key,
            feature_fingerprint=FEATURE_SCHEMA.fingerprint, codes=codes,
        )

    def predict(self, features: FeatureVector, *, decision_time=None,
                now: datetime | None = None) -> Prediction:
        decision_time = decision_time or datetime.now(timezone.utc)
        started = monotonic_ms()

        if self.mode is AIMode.DISABLED:
            return self._abstain(AbstainReason.DISABLED, decision_time)

        model = self.model
        if model is None:
            return self._abstain(AbstainReason.NO_MODEL, decision_time)

        manifest = model.manifest
        if manifest.approval_state != self.required_approval:
            return self._abstain(AbstainReason.NO_MODEL, decision_time,
                                 codes=(f"approval_state={manifest.approval_state}",))
        if manifest.expired(now):
            return self._abstain(AbstainReason.MODEL_STALE, decision_time,
                                 codes=(f"expired_at={manifest.expires_at}",))
        if manifest.symbol != self.symbol:
            return self._abstain(AbstainReason.SYMBOL_MISMATCH, decision_time,
                                 codes=(f"model_symbol={manifest.symbol}",))
        if manifest.profile_key != self.profile_key:
            return self._abstain(AbstainReason.PROFILE_MISMATCH, decision_time,
                                 codes=(f"model_profile={manifest.profile_key}",))
        if manifest.feature_fingerprint != features.schema.fingerprint:
            return self._abstain(AbstainReason.SCHEMA_MISMATCH, decision_time,
                                 codes=(f"model={manifest.feature_fingerprint}",
                                        f"features={features.schema.fingerprint}"))

        row = features.as_row()
        if row is None:
            # Missing stays missing. A hole is not imputed to zero and then
            # scored as though somebody had measured it.
            return self._abstain(AbstainReason.INCOMPLETE_FEATURES, decision_time,
                                 codes=tuple(features.missing))

        try:
            net, downside = model.predict(row)
        except Exception as exc:
            logger.exception("prediction failed")
            return self._abstain(AbstainReason.ERROR, decision_time, codes=(str(exc)[:80],))

        elapsed = monotonic_ms() - started
        if elapsed > self.time_budget_ms:
            # Over budget is an abstention even though a number came back: a
            # late answer on the protective cadence is not an answer.
            return self._abstain(AbstainReason.TIMEOUT, decision_time,
                                 codes=(f"{elapsed:.1f}ms",))

        return Prediction(
            decision_time=decision_time,
            inputs_available_at=features.inputs_available_at,
            model_version=manifest.model_version,
            profile_version=self.profile_key,
            feature_fingerprint=features.schema.fingerprint,
            predicted_net=net,
            predicted_downside=downside,
            # No probability is reported: this model was not calibrated, and an
            # uncalibrated score dressed as a probability is worse than none.
            probability_positive=None, calibrated=False,
            latency_ms=elapsed,
        )


def decide(*, deterministic_allowed: bool, deterministic_reason: str,
           predictor: EntryPredictor | None, features: FeatureVector | None,
           mode: AIMode, min_predicted_net: float = 0.0,
           decision_time=None) -> AdmissionOutcome:
    """The one place a prediction can influence admission.

    Order is the whole point. The deterministic gates run first and their
    refusal is final; the model is not consulted at all in that case. In SHADOW
    mode the prediction is recorded and the admitted decision is exactly what
    the deterministic layer said — the model changes nothing.
    """
    if not deterministic_allowed:
        # A model has no say here, and is not asked.
        return AdmissionOutcome(
            admitted=False, ai_consulted=False, prediction=None,
            deterministic_allowed=False,
            reason=f"deterministic gates refused: {deterministic_reason}",
        )

    if mode is AIMode.DISABLED or predictor is None or features is None:
        return AdmissionOutcome(
            admitted=True, ai_consulted=False, prediction=None,
            deterministic_allowed=True, reason="AI disabled; baseline decision stands",
        )

    prediction = predictor.predict(features, decision_time=decision_time)

    if mode is AIMode.SHADOW:
        # Recorded, and deliberately not acted on.
        return AdmissionOutcome(
            admitted=True, ai_consulted=True, prediction=prediction,
            deterministic_allowed=True,
            reason="shadow mode: prediction recorded, baseline decision unchanged",
        )

    # GATING is reserved and not selectable yet. If it is ever approved, an
    # abstention must BLOCK new entries rather than wave them through — an
    # AI-required profile with no AI available has not been shown to be safe.
    if prediction.abstained:
        return AdmissionOutcome(
            admitted=False, ai_consulted=True, prediction=prediction,
            deterministic_allowed=True,
            reason=f"AI required but abstained: {prediction.abstain_reason.value}",
        )
    admitted = (prediction.predicted_net or 0.0) >= min_predicted_net
    return AdmissionOutcome(
        admitted=admitted, ai_consulted=True, prediction=prediction,
        deterministic_allowed=True,
        reason=(f"predicted net {prediction.predicted_net:.2f} "
                f"{'meets' if admitted else 'below'} the {min_predicted_net:.2f} threshold"),
    )
