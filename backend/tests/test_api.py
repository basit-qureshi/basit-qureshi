"""The API surface: settings validation, persistence, and the daily figures."""

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.bot_manager as bm

    monkeypatch.setattr(bm, "_SETTINGS_FILE", tmp_path / "runtime_settings.json")
    manager = bm.BotManager()
    monkeypatch.setattr(bm, "bot_manager", manager)
    import app.api.routes as routes

    monkeypatch.setattr(routes, "bot_manager", manager)
    from app.main import app

    return TestClient(app), manager


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
