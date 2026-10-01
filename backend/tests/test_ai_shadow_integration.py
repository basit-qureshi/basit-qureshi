"""A FAKE inference component, driven through the intended shadow path.

What this file is for. Saying "AI cannot affect trading" because the engine
source contains no call to the model is a statement about a file, not about a
run. These tests put a deliberately hostile fake model through the integration
path the design intends — `decide()` in SHADOW mode, with a `ShadowRecorder`
alongside a live engine — and compare the engine's actual order requests and
configuration against a control run with no model at all.

What this file does NOT claim. **Production shadow wiring does not exist.** The
engine never calls `decide()`; a session records `ai_mode: disabled` because
that is the only mode there is. That wiring is explicitly PENDING, and the last
test here asserts its absence so the gap cannot quietly close without a test
changing. These tests show what the path does when something drives it, not
that the path is in use.
"""

import pytest

from app.ai.contracts import AIMode
from app.ai.predictor import EntryPredictor, decide
from app.ai.shadow import ShadowRecorder
from tests.conftest import MAGIC
from tests.test_phase_d_ai import complete_vector, make_model


class HostileModel:
    """A fake inference component. Answers loudly, and wrongly, every time."""

    def __init__(self, real, behaviour="reject"):
        self.manifest = real.manifest
        self.behaviour = behaviour
        self.calls = 0

    def predict(self, row):
        self.calls += 1
        if self.behaviour == "raise":
            raise RuntimeError("inference blew up")
        if self.behaviour == "reject":
            return -999.0, -999.0      # "never take anything"
        return 999.0, 0.0              # "take everything, it is free money"


def predictor_with(tmp_path, behaviour, mode=AIMode.SHADOW):
    model = HostileModel(make_model(tmp_path), behaviour)
    return EntryPredictor(model, symbol="XAUUSD", profile_key="baseline@v1", mode=mode), model


def live(**kw):
    kw.setdefault("basket_stop_loss_usd", 60.0)
    kw.setdefault("max_daily_loss_usd", 100.0)
    kw.setdefault("basket_take_profit_usd", 10_000.0)
    # The double charges neither, and an unknown closing cost now blocks new
    # exposure — the shadow engine is built directly, so it states them itself.
    kw.setdefault("exit_commission_per_lot", 0.0)
    kw.setdefault("slippage_points_per_fill", 0.0)
    return kw


def run_to_grid(broker, engine, shadow=None, predictor=None):
    """Two ticks, which is where a grid gets placed. Optionally shadowed.

    The shadow observation is taken at the same decision point the engine uses,
    which is what the intended integration would do.
    """
    for _ in range(2):
        allowed, reason = engine._entry_gate(broker.get_account_info())
        if shadow is not None:
            outcome = decide(deterministic_allowed=allowed,
                             deterministic_reason=reason or "all gates passed",
                             predictor=predictor, features=complete_vector(),
                             mode=AIMode.SHADOW)
            shadow.record(decision_time=outcome.prediction.decision_time
                          if outcome.prediction else None,
                          prediction=outcome.prediction.as_dict() if outcome.prediction else {},
                          baseline_admitted=allowed,
                          model_would_admit=None)
            assert outcome.admitted == allowed, (
                "shadow mode changed the deterministic decision"
            )
        engine._tick()
        broker.next_candle()
    return sorted(o.price for o in broker.get_pending_orders("XAUUSD", magic=MAGIC))


def config_snapshot(engine) -> dict:
    keys = ("lot_size", "grid_distance", "buy_stop_levels", "sell_stop_levels",
            "basket_take_profit_usd", "basket_stop_loss_usd", "max_daily_loss_usd",
            "capital_floor_usd", "max_equity_drawdown_percent", "magic_number",
            "max_open_positions", "capital_reserve_percent")
    return {k: getattr(engine, k) for k in keys}


# --- the same run, with and without a hostile model watching ------------------

@pytest.mark.parametrize("behaviour", ["reject", "accept", "raise"])
def test_a_hostile_fake_model_changes_no_order_and_no_setting(behaviour, broker,
                                                              engine_factory, tmp_path):
    from app.engine.grid_engine import GridEngine
    from tests.conftest import FakeBroker

    control_orders = run_to_grid(broker, engine_factory(**live()))
    assert control_orders, "fixture: the control run must place a grid"

    # A second, independent broker and engine, identically configured, so the
    # only difference between the two runs is the fake model watching one.
    shadow_broker = FakeBroker()
    shadow_engine = GridEngine(broker=shadow_broker, symbol="XAUUSD", mode="demo",
                               magic_number=MAGIC, capital_floor_usd=50.0, **live())
    predictor, model = predictor_with(tmp_path, behaviour)
    recorder = ShadowRecorder()

    before = config_snapshot(shadow_engine)
    shadow_orders = run_to_grid(shadow_broker, shadow_engine, recorder, predictor)

    assert shadow_orders == control_orders, (
        f"a {behaviour} model changed the orders the engine requested"
    )
    assert config_snapshot(shadow_engine) == before, "a model mutated configuration"
    assert len(recorder.records) == 2, "the shadow path did not actually run"
    if behaviour != "raise":
        assert model.calls > 0, "the fake model was never consulted"


def test_a_model_that_raises_abstains_instead_of_breaking_the_decision(tmp_path):
    predictor, _ = predictor_with(tmp_path, "raise")
    outcome = decide(deterministic_allowed=True, deterministic_reason="all gates passed",
                     predictor=predictor, features=complete_vector(), mode=AIMode.SHADOW)
    assert outcome.admitted is True, "a broken model blocked a permitted entry"
    assert outcome.prediction.abstained is True


def test_a_rejecting_model_cannot_veto_in_shadow_mode(tmp_path):
    predictor, model = predictor_with(tmp_path, "reject")
    outcome = decide(deterministic_allowed=True, deterministic_reason="all gates passed",
                     predictor=predictor, features=complete_vector(), mode=AIMode.SHADOW)
    assert outcome.admitted is True
    assert outcome.prediction.predicted_net == -999.0, "the fake model was not consulted"
    assert "unchanged" in outcome.reason


def test_an_accepting_model_cannot_overrule_a_deterministic_refusal(tmp_path):
    predictor, _ = predictor_with(tmp_path, "accept", mode=AIMode.GATING)
    outcome = decide(deterministic_allowed=False,
                     deterministic_reason="NO_TRADE: capital floor reached",
                     predictor=predictor, features=complete_vector(), mode=AIMode.GATING)
    assert outcome.admitted is False
    assert outcome.ai_consulted is False, "the model was consulted despite a hard refusal"


def test_the_shadow_ledger_never_reports_a_model_profit(tmp_path):
    """A skipped loss is a counterfactual, and counterfactuals stay labelled."""
    predictor, _ = predictor_with(tmp_path, "reject")
    recorder = ShadowRecorder()
    outcome = decide(deterministic_allowed=True, deterministic_reason="ok",
                     predictor=predictor, features=complete_vector(), mode=AIMode.SHADOW)
    recorder.record(decision_time=outcome.prediction.decision_time,
                    prediction=outcome.prediction.as_dict(),
                    baseline_admitted=True, model_would_admit=False)
    summary = recorder.summary()
    # The ledger reports COVERAGE, not a model P&L. `observed_net_total` is the
    # baseline's own realised result; there is deliberately no field that adds up
    # what the model "would have made".
    assert "observed_net_total" in summary
    for invented in ("model_net", "model_profit", "counterfactual_net", "saved"):
        assert invented not in summary, f"the ledger reported {invented}"
    assert summary["opportunities_recorded"] == 1
    assert summary["observed_outcomes_linked"] == 0, (
        "nothing resolved yet, so nothing may be scored"
    )


# --- the gap, asserted so it cannot close silently ---------------------------

def test_production_shadow_wiring_is_absent_and_that_is_recorded(request):
    """A statement about the FILE, not a proof of runtime isolation.

    The runtime evidence is the comparison above. This test exists so that the
    day somebody wires `decide()` into the engine, a test fails and the status
    label has to be revisited rather than drifting.
    """
    from pathlib import Path

    source = Path(request.config.rootdir) / "app" / "engine" / "grid_engine.py"
    text = source.read_text()
    assert "decide(" not in text and "EntryPredictor" not in text, (
        "the engine now consults a model: update the AI status label, and test "
        "the wiring end to end rather than relying on this assertion"
    )
