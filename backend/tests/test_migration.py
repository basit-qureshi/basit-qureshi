"""Existing databases must keep working, and keep their history."""

import sqlite3

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker


def test_an_old_database_gains_the_new_columns_and_keeps_its_rows(tmp_path, monkeypatch):
    """A database written before magic/trading_day existed is upgraded in
    place. Dropping and recreating the table would take the user's trade
    history with it."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticket VARCHAR, symbol VARCHAR, side VARCHAR,
            volume FLOAT, open_price FLOAT, close_price FLOAT, sl FLOAT, tp FLOAT, profit FLOAT,
            mode VARCHAR, status VARCHAR, open_time DATETIME, close_time DATETIME)"""
    )
    conn.execute(
        "INSERT INTO trades (ticket, symbol, side, volume, open_price, sl, tp, profit, mode, status)"
        " VALUES ('old1', 'XAUUSD', 'BUY', 0.01, 4000.0, 0, 0, 1.23, 'demo', 'CLOSED')"
    )
    conn.commit()
    conn.close()

    from app import db as db_module

    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    db_module.init_db()

    columns = {c["name"] for c in inspect(engine).get_columns("trades")}
    assert {"magic", "trading_day"} <= columns

    with db_module.SessionLocal() as session:
        rows = session.query(db_module.TradeRecord).all()
    assert len(rows) == 1, "the existing trade history was lost"
    assert rows[0].ticket == "old1"
    assert rows[0].profit == 1.23
    assert rows[0].magic is None  # unknown for a pre-migration row, not guessed

    db_module.init_db()  # running it twice must be harmless
    assert len(inspect(engine).get_columns("trades")) == len(columns)
    engine.dispose()


def test_pre_migration_rows_do_not_reach_the_daily_figures(tmp_path, monkeypatch):
    """A row with no magic number belongs to no bot identity, so it cannot move
    a daily target that is scoped to one."""
    from app import db as db_module
    from app.db import TradeRecord

    path = tmp_path / "mixed.db"
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    db_module.init_db()

    with db_module.SessionLocal() as session:
        session.add(TradeRecord(ticket="legacy", symbol="XAUUSD", side="BUY", volume=0.01,
                                open_price=4000.0, sl=0, tp=0, profit=999.0, mode="demo",
                                status="CLOSED"))
        session.add(TradeRecord(ticket="mine", symbol="XAUUSD", side="BUY", volume=0.01,
                                open_price=4000.0, sl=0, tp=0, profit=2.0, mode="demo",
                                status="CLOSED", magic=990022, trading_day="2026-01-05"))
        session.commit()

    totals = db_module.daily_totals("XAUUSD", "demo", 990022, "2026-01-05")
    assert totals.net == 2.0
    engine.dispose()
