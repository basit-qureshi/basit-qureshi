"""The API surface: settings validation, persistence, and the daily figures."""

import json

# `client` comes from conftest so every API test shares the same isolation:
# a settings file under tmp_path and a broker that is always the mock.


def test_daily_target_saves_and_persists(client, tmp_path):
    c, manager = client
    assert c.get("/api/status").json()["settings"]["grid_daily_profit_target_usd"] == 0.0

    r = c.post("/api/settings", json={"grid_daily_profit_target_usd": 25.5})
    assert r.status_code == 200
    assert r.json()["grid_daily_profit_target_usd"] == 25.5
    assert manager.engine.daily_profit_target_usd == 25.5

    saved = json.loads((tmp_path / "runtime_settings.json").read_text())
    assert saved["grid_daily_profit_target_usd"] == 25.5


def test_a_negative_daily_target_is_rejected(client):
    c, _ = client
    assert c.post("/api/settings", json={"grid_daily_profit_target_usd": -5}).status_code == 422


def test_zero_disables_the_daily_target(client):
    c, manager = client
    c.post("/api/settings", json={"grid_daily_profit_target_usd": 0})
    assert manager.engine.daily_profit_target_usd == 0
    assert manager.engine._check_daily_target([]) is False


def test_stats_exposes_split_daily_figures(client):
    c, manager = client
    from app import db as db_module
    from app.db import TradeRecord

    day = manager.engine._trading_day_for(manager.engine._current_candle_time())
    manager.engine._trading_day = day
    with db_module.SessionLocal() as session:
        for ticket, profit in (("a", 15.0), ("b", -4.0), ("c", -6.0)):
            session.add(
                TradeRecord(
                    ticket=ticket, symbol=manager.settings["symbol"], side="BUY", volume=0.01,
                    open_price=4000.0, sl=0.0, tp=0.0, profit=profit,
                    mode=manager.settings["mode"], status="CLOSED",
                    magic=manager.settings["grid_magic_number"], trading_day=day,
                )
            )
        session.commit()
    manager.engine._refresh_daily_totals()

    stats = c.get("/api/stats").json()
    assert stats["today_gross_profit_usd"] == 15.0
    assert stats["today_gross_loss_usd"] == 10.0
    assert stats["today_net_profit_usd"] == 5.0
    # the old field is kept and equals the net, so nothing that reads it breaks
    assert stats["today_profit"] == stats["today_net_profit_usd"]


def test_timezone_setting_reaches_the_engine(client):
    c, manager = client
    assert manager.settings["timezone"] == "Asia/Karachi"
    assert manager.engine.timezone_name == "Asia/Karachi"
    assert c.get("/api/status").json()["timezone"] == "Asia/Karachi"


# --- session recording --------------------------------------------------------
# Starting a RECORDING is not starting the bot, and stopping one is not stopping
# the bot. These tests pin that separation down, because confusing the two would
# either leave a session unrecorded or stop protection to end a recording.


def evidence_dir(tmp_path, monkeypatch):
    import app.api.routes as routes

    monkeypatch.setattr(routes.settings, "evidence_dir", str(tmp_path / "evidence"))
    return tmp_path / "evidence"


def test_starting_a_recording_does_not_start_trading(client, tmp_path, monkeypatch):
    c, manager = client
    directory = evidence_dir(tmp_path, monkeypatch)
    was_running = manager.engine.running

    body = c.post("/api/session/start").json()
    assert body["ok"] is True
    assert manager.engine.running is was_running, "recording must not start the bot"
    assert (directory / f"{body['session']['session_id']}.manifest.json").exists()


def test_a_recording_over_a_mock_broker_is_never_labelled_verified(client, tmp_path, monkeypatch):
    c, _ = client
    evidence_dir(tmp_path, monkeypatch)
    session = c.post("/api/session/start").json()["session"]
    assert session["account_type"] == "unverified"
    assert session["ai_mode"] == "disabled"
    assert session["profile_frozen"] is True


def test_a_second_start_refuses_rather_than_splitting_the_evidence(client, tmp_path, monkeypatch):
    c, _ = client
    evidence_dir(tmp_path, monkeypatch)
    first = c.post("/api/session/start").json()
    second = c.post("/api/session/start").json()
    assert second["ok"] is False
    assert first["session"]["session_id"] in second["reason"]

    replaced = c.post("/api/session/start?replace=true").json()
    assert replaced["ok"] is True
    assert replaced["session"]["session_id"] != first["session"]["session_id"]


def test_stopping_a_recording_leaves_the_engine_alone(client, tmp_path, monkeypatch):
    c, manager = client
    evidence_dir(tmp_path, monkeypatch)
    c.post("/api/session/start")
    was_running = manager.engine.running

    body = c.post("/api/session/stop").json()
    assert body["ok"] is True
    assert manager.engine.running is was_running
    assert c.get("/api/session/status").json()["active"] is False


def test_the_packet_endpoint_returns_a_redacted_copy(client, tmp_path, monkeypatch):
    c, manager = client
    evidence_dir(tmp_path, monkeypatch)
    c.post("/api/session/start")
    manager.engine.evidence.record("quote_observed", bid=4000.0, mt5_password="hunter2")

    packet = c.get("/api/session/packet").json()
    assert "hunter2" not in json.dumps(packet)
    assert packet["events"][0]["mt5_password"] == "<redacted>"


def test_the_packet_endpoint_is_harmless_with_no_session(client):
    c, _ = client
    packet = c.get("/api/session/packet").json()
    assert packet["events"] == []
    assert c.get("/api/session/status").json()["active"] is False
