"""Filtering, paging and the reason a trade ended.

The dashboard was showing a truncated, unfilterable list, which is no use for
reviewing what the bot did. These tests pin the behaviour that makes the
history reviewable: every row reachable, filters that narrow the same query
the totals come from, and a stated reason for each close.
"""

from datetime import datetime, timezone

import pytest

from app import db as db_module
from app.db import TradeRecord


def seed(manager, rows):
    """rows: (ticket, side, profit, status, trading_day, close_reason)"""
    with db_module.SessionLocal() as session:
        for i, (ticket, side, profit, status, day, reason) in enumerate(rows):
            session.add(
                TradeRecord(
                    ticket=ticket, account_id=manager.engine._account_id,
                    symbol=manager.settings["symbol"], side=side, volume=0.01,
                    open_price=4000.0 + i, sl=0.0, tp=0.0, profit=profit,
                    mode=manager.settings["mode"], status=status,
                    magic=manager.settings["grid_magic_number"], trading_day=day,
                    close_reason=reason,
                    # The open time is derived from the trading day so the two
                    # cannot drift apart and make the ordering arbitrary.
                    open_time=datetime.strptime(day, "%Y-%m-%d").replace(minute=i, hour=10),
                )
            )
        session.commit()


ROWS = [
    ("t1", "BUY", 12.0, "CLOSED", "2026-09-15", "basket target reached (+12.00)"),
    ("t2", "SELL", -4.0, "CLOSED", "2026-09-16", "basket stop hit (-4.00)"),
    ("t3", "BUY", -6.0, "CLOSED", "2026-09-17", "risk protection: daily loss limit reached"),
    ("t4", "SELL", 3.0, "CLOSED", "2026-09-17", "basket target reached (+3.00)"),
    ("t5", "BUY", None, "OPEN", "2026-09-17", None),
]


def test_every_row_is_reachable_by_paging(client):
    """The old endpoint returned one capped list. If the account has more
    trades than the cap, the rest simply could not be seen."""
    c, manager = client
    seed(manager, ROWS)

    seen = []
    first = c.get("/api/trades?page=1&page_size=2").json()
    assert first["total"] == 5
    assert first["pages"] == 3
    for page in range(1, first["pages"] + 1):
        body = c.get(f"/api/trades?page={page}&page_size=2").json()
        seen += [t["ticket"] for t in body["items"]]

    assert sorted(seen) == ["t1", "t2", "t3", "t4", "t5"]
    assert len(seen) == len(set(seen)), "a trade appeared on two pages"


def test_a_page_past_the_end_returns_the_last_page(client):
    c, manager = client
    seed(manager, ROWS)
    body = c.get("/api/trades?page=99&page_size=2").json()
    assert body["page"] == body["pages"] == 3
    assert body["items"], "asking past the end should not return an empty screen"


def test_a_date_range_selects_broker_trading_days(client):
    """Filtering has to divide days exactly where the daily accounting does,
    or a day's rows and that day's totals would disagree."""
    c, manager = client
    seed(manager, ROWS)

    body = c.get("/api/trades?date_from=2026-09-17&date_to=2026-09-17").json()
    assert sorted(t["ticket"] for t in body["items"]) == ["t3", "t4", "t5"]

    body = c.get("/api/trades?date_from=2026-09-15&date_to=2026-09-16").json()
    assert sorted(t["ticket"] for t in body["items"]) == ["t1", "t2"]


def test_filters_narrow_the_selection(client):
    c, manager = client
    seed(manager, ROWS)

    assert [t["ticket"] for t in c.get("/api/trades?side=BUY").json()["items"]] == ["t5", "t3", "t1"]
    assert [t["ticket"] for t in c.get("/api/trades?status=OPEN").json()["items"]] == ["t5"]
    assert sorted(t["ticket"] for t in c.get("/api/trades?result=win").json()["items"]) == ["t1", "t4"]
    assert sorted(t["ticket"] for t in c.get("/api/trades?result=loss").json()["items"]) == ["t2", "t3"]
    assert [t["ticket"] for t in c.get("/api/trades?result=unsettled").json()["items"]] == ["t5"]


def test_the_totals_describe_the_whole_selection_not_the_page(client):
    """Paging must not appear to change the result. The summary is computed
    over everything the filters match."""
    c, manager = client
    seed(manager, ROWS)

    page = c.get("/api/trades?page=1&page_size=1").json()
    assert len(page["items"]) == 1
    sel = page["selection"]
    assert sel["settled"] == 4
    assert sel["gross_profit"] == 15.0      # 12 + 3
    assert sel["gross_loss"] == -10.0       # -4 + -6
    assert sel["net_profit"] == 5.0
    assert sel["wins"] == 2 and sel["losses"] == 2
    assert sel["unsettled"] == 1

    # and it tracks the filters
    only_wins = c.get("/api/trades?result=win").json()["selection"]
    assert only_wins["net_profit"] == 15.0 and only_wins["losses"] == 0


def test_the_reason_a_trade_ended_is_returned(client):
    """'What happened' without 'why' is not reviewable."""
    c, manager = client
    seed(manager, ROWS)
    by_ticket = {t["ticket"]: t for t in c.get("/api/trades").json()["items"]}

    assert "basket target reached" in by_ticket["t1"]["close_reason"]
    assert "basket stop hit" in by_ticket["t2"]["close_reason"]
    assert "daily loss limit" in by_ticket["t3"]["close_reason"]
    assert by_ticket["t5"]["close_reason"] is None, "an open trade has not ended and must not claim a reason"


def test_searching_matches_ticket_or_reason(client):
    c, manager = client
    seed(manager, ROWS)
    assert [t["ticket"] for t in c.get("/api/trades?search=daily loss").json()["items"]] == ["t3"]
    assert [t["ticket"] for t in c.get("/api/trades?search=t2").json()["items"]] == ["t2"]


def test_trading_days_lists_only_days_that_exist(client):
    c, manager = client
    seed(manager, ROWS)
    body = c.get("/api/trading-days").json()
    assert body["days"] == ["2026-09-17", "2026-09-16", "2026-09-15"]
    assert body["first"] == "2026-09-15" and body["last"] == "2026-09-17"


def test_a_bad_date_is_rejected_rather_than_ignored(client):
    """Silently ignoring an unparseable date would show the wrong rows under a
    filter the owner believes is applied."""
    c, manager = client
    seed(manager, ROWS)
    assert c.get("/api/trades?date_from=15-09-2026").status_code == 400


def test_the_old_limit_parameter_still_works(client):
    c, manager = client
    seed(manager, ROWS)
    body = c.get("/api/trades?limit=2").json()
    assert len(body["items"]) == 2 and body["total"] == 5


# --- the engine actually writes the reason ----------------------------------

def test_the_engine_stamps_why_the_basket_closed(broker, engine_factory):
    """The reason has to come from the engine, not be invented by the API."""
    from tests.conftest import MAGIC

    e = engine_factory(basket_take_profit_usd=1.0, basket_stop_loss_usd=60.0)
    e._tick()
    broker.next_candle()
    e._tick()

    broker.price += 4.0          # fill the buy side into profit
    broker.next_candle()
    e._tick()
    broker.next_candle()
    e._tick()

    with db_module.SessionLocal() as session:
        closed = session.query(TradeRecord).filter(
            TradeRecord.magic == MAGIC, TradeRecord.status == "CLOSED"
        ).all()
    assert closed, "the basket should have reached its target and closed"
    assert all(r.close_reason for r in closed), "a closed trade with no stated reason"
    assert any("target" in r.close_reason.lower() for r in closed)


def test_the_first_reason_is_not_overwritten(broker, engine_factory):
    """A later sweep finding the same ticket already gone must not replace the
    explanation that actually ended it."""
    from tests.conftest import MAGIC

    # The target is out of reach, so nothing closes on its own and the only
    # reason written is the one this test states.
    e = engine_factory(basket_take_profit_usd=10_000.0, basket_stop_loss_usd=60.0)
    e._tick()
    broker.next_candle()
    e._tick()
    broker.price += 4.0
    broker.next_candle()
    e._tick()

    positions = broker.get_open_positions("XAUUSD", magic=MAGIC)
    e._close_everything(positions, broker.get_pending_orders("XAUUSD", magic=MAGIC), "first reason")
    e._settle_closed_trades("second reason")

    with db_module.SessionLocal() as session:
        rows = session.query(TradeRecord).filter(
            TradeRecord.magic == MAGIC, TradeRecord.close_reason.isnot(None)
        ).all()
    assert rows
    assert all(r.close_reason == "first reason" for r in rows)


# --- open trades panel -------------------------------------------------------

def test_open_trades_are_priced_net_and_sorted_worst_first(client):
    """The panel exists so the owner can see what is actually being held. The
    worst position is the one worth seeing without scrolling."""
    c, manager = client
    broker = manager.broker

    body = c.get("/api/open-trades").json()
    assert body["connected"] is True
    assert body["positions"] == []
    assert body["totals"]["count"] == 0


def test_open_trades_report_costs_and_the_figure_the_basket_is_judged_on(broker, engine_factory, monkeypatch):
    """Gross is not what the basket rule reads, so it is not what the panel
    may show on its own."""
    import types

    import app.api.routes as routes
    from fastapi.testclient import TestClient

    e = engine_factory(basket_stop_loss_usd=60.0)
    pos = broker.open_position("BUY", broker.price - 5.0)
    pos.commission = -0.70
    pos.swap = -0.30

    monkeypatch.setattr(routes, "bot_manager", types.SimpleNamespace(engine=e, broker=broker))
    from app.main import app

    body = TestClient(app).get("/api/open-trades").json()
    assert body["totals"]["count"] == 1
    assert body["totals"]["gross_profit"] == 5.0
    assert body["totals"]["net_profit"] == 4.0          # after 1.00 of costs
    assert body["totals"]["after_exit_cost"] < body["totals"]["net_profit"]
    item = body["positions"][0]
    assert item["swap"] == -0.30 and item["commission"] == -0.70
    assert item["net_profit"] == 4.0
