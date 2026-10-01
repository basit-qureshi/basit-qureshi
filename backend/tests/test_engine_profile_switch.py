"""Switching engines: when it is allowed, and the four states where it is not.

The app now starts on the **original** engine, because that is the bot the owner
asked to be running. Switching to the guarded one is the opt-in.

Each refusal below exists because switching through that state would lose
something. They are tested from the API, because that is where the toggle is
pressed, and from the manager, because that is where the decision is made.
"""

import pytest

from app.brokers.base import PendingType
from app.engine import profiles as engine_profiles
from app.engine.grid_engine import GridEngine
from app.engine.original_adapter import OriginalEngine


def _switch(client, profile):
    return client.post("/api/engine-profile", json={"profile": profile})


# ------------------------------------------------------------- the happy path

def test_the_app_starts_on_the_original_engine(client):
    api, manager = client
    assert manager.engine_profile() == "original"
    assert isinstance(manager.engine, OriginalEngine)
    assert api.get("/api/engine-profile").json()["active"] == "original"
    assert api.get("/api/status").json()["engine_profile"] == "original"


def test_switching_to_guarded_builds_the_guarded_engine(client):
    api, manager = client
    body = _switch(api, "guarded").json()
    assert body["ok"] is True and body["changed"] is True
    assert manager.engine_profile() == "guarded"
    assert isinstance(manager.engine, GridEngine)
    assert api.get("/api/status").json()["engine_profile"] == "guarded"


def test_switching_back_restores_the_original_engine(client):
    api, manager = client
    _switch(api, "guarded")
    _switch(api, "original")
    assert isinstance(manager.engine, OriginalEngine)
    assert api.get("/api/status").json()["engine_profile"] == "original"


def test_switching_to_the_engine_already_running_changes_nothing(client):
    api, manager = client
    before = manager.engine
    body = _switch(api, "original").json()
    assert body["changed"] is False
    assert manager.engine is before


def test_the_choice_survives_a_manager_rebuild(client, tmp_path, monkeypatch):
    """A restart must come back on the engine that was selected, not the default."""
    api, manager = client
    _switch(api, "guarded")

    import app.bot_manager as bm
    rebuilt = bm.BotManager()
    assert rebuilt.engine_profile() == "guarded"
    assert isinstance(rebuilt.engine, GridEngine)


def test_an_unknown_profile_is_rejected_rather_than_defaulted(client):
    api, _ = client
    response = _switch(api, "whatever")
    assert response.status_code == 400
    assert "unknown engine profile" in response.json()["detail"]


# ----------------------------------------------------------- the four refusals

def test_switching_is_refused_while_the_bot_is_running(client):
    api, manager = client
    manager.engine._running = True
    response = _switch(api, "guarded")
    assert response.status_code == 409
    assert "Stop the bot" in response.json()["detail"]
    assert manager.engine_profile() == "original"


def test_switching_is_refused_while_this_bot_owns_exposure(client):
    api, manager = client
    broker = manager.engine.broker
    # Far above the price so the double does not fill it; a resting order is
    # exposure for this purpose whether or not it has triggered.
    broker.place_pending_order("XAUUSD", PendingType.BUY_STOP, 0.01, 9_000_000.0,
                               "", manager.engine.magic_number)
    response = _switch(api, "guarded")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "still" in detail and "open" in detail
    assert manager.engine_profile() == "original"


def test_switching_is_refused_when_the_broker_cannot_be_read(client):
    """Unknown is not flat."""
    api, manager = client

    def unreadable(*_a, **_k):
        raise RuntimeError("terminal not answering")

    manager.engine.broker.get_open_positions = unreadable
    response = _switch(api, "guarded")
    assert response.status_code == 409
    assert "not an empty one" in response.json()["detail"]


def test_switching_is_refused_while_the_active_engine_is_halted(client):
    api, manager = client
    manager.engine._halt_reason = "daily loss limit reached"
    response = _switch(api, "guarded")
    assert response.status_code == 409
    assert "not a way past a halt" in response.json()["detail"]
    assert manager.engine_profile() == "original"


def test_switching_INTO_a_halted_guarded_engine_is_refused(client):
    """The guarded engine's halt is durable, so it would arrive already halted.

    A halt is raised there, the owner switches away, and then tries to come
    back. Coming back is what is refused — arriving at an engine that is halted
    with nothing on screen saying why is worse than being told now.
    """
    api, manager = client
    _switch(api, "guarded")
    manager.engine._halt_reason = "equity drawdown limit reached"
    manager.engine._persist_risk_state()
    manager.engine._halt_reason = None      # as if the process had restarted
    assert _switch(api, "original").status_code == 200

    response = _switch(api, "guarded")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "unresolved halt" in detail
    assert "would start halted" in detail


def test_switching_AWAY_from_a_halted_engine_is_refused(client):
    """The other direction, and the more important one."""
    api, manager = client
    manager.engine._halt_reason = "daily loss limit reached"
    response = _switch(api, "guarded")
    assert response.status_code == 409
    assert "not a way past a halt" in response.json()["detail"]


def test_the_original_engine_has_no_stored_halt_to_block_a_switch(client):
    """It keeps none, so it cannot block — and the manager says so by returning
    None rather than guessing at a key that was never written."""
    _, manager = client
    assert manager._stored_halt_reason("original") is None


def test_an_unreadable_risk_record_refuses_the_switch(client, monkeypatch):
    api, manager = client
    import app.bot_manager as bm

    def boom(_key):
        raise RuntimeError("database locked")

    monkeypatch.setattr(bm.db_module, "load_risk", boom)
    response = _switch(api, "guarded")
    assert response.status_code == 409
    assert "UNKNOWN" in response.json()["detail"]


# -------------------------------------------------------- nothing else moves

def test_switching_preserves_settings_and_history(client):
    api, manager = client
    api.post("/api/settings", json={"grid_capital_floor_usd": 170.0, "grid_lot_size": 0.02})
    before = dict(manager.settings)

    _switch(api, "guarded")
    after = {k: v for k, v in manager.settings.items() if k != "engine_profile"}
    assert after == {k: v for k, v in before.items() if k != "engine_profile"}
    assert manager.settings["grid_capital_floor_usd"] == 170.0


def test_the_original_engine_names_the_settings_it_does_not_read(client):
    api, manager = client
    api.post("/api/settings", json={"grid_capital_floor_usd": 170.0})
    status = api.get("/api/status").json()
    assert status["engine_profile"] == "original"
    assert "capital_floor_usd" in status["settings_not_applied"]
    assert "capital_reserve_percent" in status["settings_not_applied"]
    # Still stored, and still applies again on the guarded engine.
    assert status["settings"]["grid_capital_floor_usd"] == 170.0


def test_the_profile_listing_describes_both_without_a_profit_claim(client):
    api, _ = client
    body = api.get("/api/engine-profile").json()
    keys = {p["key"] for p in body["profiles"]}
    assert keys == set(engine_profiles.ALL_PROFILES)
    assert "Neither profile has been shown to be profitable" in body["note"]
    assert body["switchable"] is True


def test_the_listing_reports_a_running_bot_as_not_switchable(client):
    api, manager = client
    manager.engine._running = True
    assert api.get("/api/engine-profile").json()["switchable"] is False


# ------------------------------------- the endpoints that differ per profile

def test_pause_entries_refuses_on_the_original_and_works_on_the_guarded(client):
    api, manager = client
    body = api.post("/api/pause-entries").json()
    assert body["ok"] is False
    assert "no owner pause" in body["message"]

    _switch(api, "guarded")
    assert api.post("/api/pause-entries").json()["ok"] is True


def test_resume_entries_returns_a_conflict_on_the_original(client):
    api, _ = client
    response = api.post("/api/resume-entries")
    assert response.status_code == 409
    assert "no owner pause" in response.json()["detail"]


def test_clear_halt_works_on_the_original_and_names_what_it_is(client):
    api, manager = client
    manager.engine._halt_reason = "daily loss limit reached"
    body = api.post("/api/clear-halt").json()
    assert body["ok"] is True
    assert "no durable halt" in body["message"]


def test_open_trades_answers_under_both_engines(client):
    """It reads engine internals that only the guarded engine originally had."""
    api, _ = client
    for profile in ("original", "guarded"):
        _switch(api, profile)
        body = api.get("/api/open-trades").json()
        assert body["connected"] is True
        assert body["totals"]["count"] == 0
        assert "day_risk" in body

    _switch(api, "original")
    totals = api.get("/api/open-trades").json()
    assert totals["day_risk"]["available"] is False
    assert totals["totals"]["exit_cost_known"] is False


def test_stats_and_trades_answer_under_the_original(client):
    api, _ = client
    assert api.get("/api/stats").status_code == 200
    assert api.get("/api/trades").status_code == 200
    assert api.get("/api/session/status").status_code == 200
    assert api.get("/api/candles?count=5").status_code == 200
