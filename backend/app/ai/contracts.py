"""What a model is allowed to say, and what that does and does not authorize.

The learned component has exactly one job: score whether an **otherwise
eligible** new basket is worth considering under a **fixed** strategy profile.

It cannot place an order, pick a direction, change the lot, move the spacing,
touch a stop, or release an entry block. Protective exits stay deterministic
and never consult a model. A `Prediction` is an opinion with provenance
attached; `AdmissionOutcome` is what the deterministic layer decides to do
about it, and that layer runs the capital checks either way.

Abstention is a first-class answer. A model that is missing, stale,
schema-incompatible, slow, or handed incomplete features **abstains with a
reason** — it does not guess, and a guess is never safer than a refusal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class AbstainReason(str, Enum):
    NO_MODEL = "no_model_loaded"
    MODEL_STALE = "model_past_its_expiry"
    SCHEMA_MISMATCH = "feature_schema_does_not_match_model"
    INCOMPLETE_FEATURES = "required_features_missing_or_unknown"
    PROFILE_MISMATCH = "model_not_compatible_with_this_profile"
    SYMBOL_MISMATCH = "model_not_trained_for_this_symbol"
    TIMEOUT = "prediction_exceeded_its_time_budget"
    ERROR = "prediction_raised"
    DISABLED = "ai_disabled"


@dataclass(frozen=True)
class FeatureSchema:
    """The ordered feature contract, shared by training and serving.

    Order matters: a model scores a vector, and a vector assembled in a
    different order is a different question with the same numbers in it. The
    fingerprint is checked at load and at every prediction.
    """

    names: tuple[str, ...]
    version: str

    @property
    def fingerprint(self) -> str:
        import hashlib
        payload = "|".join(self.names) + f"#{self.version}"
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def as_dict(self) -> dict:
        return {"names": list(self.names), "version": self.version,
                "fingerprint": self.fingerprint}


@dataclass(frozen=True)
class Prediction:
    """One model output, with everything needed to audit it later.

    A valid Prediction is NOT a trading authorization. It carries no side, no
    size and no order intent, because the model has no opinion about any of
    those and is not permitted to acquire one.
    """

    decision_time: datetime
    #: When the newest input was actually available. Not the same as
    #: decision_time — a prediction made now on a ten-minute-old feature is a
    #: ten-minute-old opinion, and this is what makes that visible.
    inputs_available_at: datetime | None
    model_version: str
    profile_version: str
    feature_fingerprint: str
    #: Estimated net result of the basket, in account currency. This is a
    #: MAGNITUDE, not a win probability: with losses that can exceed wins, "how
    #: often" is the wrong question on its own.
    predicted_net: float | None = None
    #: Estimated downside (worst marked excursion) for the same basket.
    predicted_downside: float | None = None
    #: Optional probability the basket ends positive. Present only when the
    #: model was calibrated; an uncalibrated score is not reported as one.
    probability_positive: float | None = None
    calibrated: bool = False
    abstained: bool = False
    abstain_reason: AbstainReason | None = None
    reason_codes: tuple[str, ...] = ()
    latency_ms: float | None = None

    @property
    def usable(self) -> bool:
        return not self.abstained and self.predicted_net is not None

    def as_dict(self) -> dict:
        return {
            "decision_time": self.decision_time.isoformat(),
            "inputs_available_at": self.inputs_available_at.isoformat() if self.inputs_available_at else None,
            "model_version": self.model_version,
            "profile_version": self.profile_version,
            "feature_fingerprint": self.feature_fingerprint,
            "predicted_net": self.predicted_net,
            "predicted_downside": self.predicted_downside,
            "probability_positive": self.probability_positive,
            "calibrated": self.calibrated,
            "abstained": self.abstained,
            "abstain_reason": self.abstain_reason.value if self.abstain_reason else None,
            "reason_codes": list(self.reason_codes),
            "latency_ms": self.latency_ms,
        }

    @classmethod
    def abstain(cls, reason: AbstainReason, *, decision_time=None, model_version="none",
                profile_version="none", feature_fingerprint="none", codes=()) -> "Prediction":
        return cls(
            decision_time=decision_time or datetime.now(timezone.utc),
            inputs_available_at=None, model_version=model_version,
            profile_version=profile_version, feature_fingerprint=feature_fingerprint,
            abstained=True, abstain_reason=reason, reason_codes=tuple(codes),
        )


@dataclass(frozen=True)
class AdmissionOutcome:
    """What the deterministic layer did with a prediction.

    `ai_consulted` says a model was asked. `admitted` is the final answer, and
    it is False whenever the deterministic gates say so — a model cannot turn
    a block into an allow. That is asserted by a test.
    """

    admitted: bool
    ai_consulted: bool
    prediction: Prediction | None
    deterministic_allowed: bool
    reason: str

    def as_dict(self) -> dict:
        return {
            "admitted": self.admitted,
            "ai_consulted": self.ai_consulted,
            "deterministic_allowed": self.deterministic_allowed,
            "reason": self.reason,
            "prediction": self.prediction.as_dict() if self.prediction else None,
        }


class AIMode(str, Enum):
    """Default is OFF. Shadow records and changes nothing."""

    DISABLED = "disabled"
    SHADOW = "shadow"
    #: Reserved. Not selectable until a model has forward evidence and the
    #: owner approves it; a profile in this mode must BLOCK new entries when
    #: the model is unavailable, while continuing to manage open exposure.
    GATING = "gating"
