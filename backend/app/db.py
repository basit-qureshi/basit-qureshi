from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import create_engine, inspect, text, String, Float, DateTime, Integer
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


class TradeRecord(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket: Mapped[str] = mapped_column(String, index=True)
    symbol: Mapped[str] = mapped_column(String)
    side: Mapped[str] = mapped_column(String)
    volume: Mapped[float] = mapped_column(Float)
    open_price: Mapped[float] = mapped_column(Float)
    close_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    sl: Mapped[float] = mapped_column(Float)
    tp: Mapped[float] = mapped_column(Float)
    profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    mode: Mapped[str] = mapped_column(String)  # demo or real
    status: Mapped[str] = mapped_column(String, default="OPEN")  # OPEN or CLOSED
    open_time: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    close_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Which program opened the trade. Daily accounting is scoped by this so a
    # manual order, a connectivity test order, or another EA can never move the
    # bot's own daily numbers.
    magic: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # The broker trading day the trade was settled on, as YYYY-MM-DD, taken from
    # the broker's own M1 candle stamp rather than from the machine clock. It is
    # written once at settlement so the engine, the dashboard and a restarted
    # process all read the same day for the same trade.
    trading_day: Mapped[str | None] = mapped_column(String, nullable=True, index=True)


engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

# Columns added after the first release. SQLite cannot add them through
# create_all() on a table that already exists, and dropping the table would
# throw away the user's trade history, so they are added in place.
_ADDED_COLUMNS = {"magic": "INTEGER", "trading_day": "VARCHAR"}


def _migrate(bind) -> None:
    inspector = inspect(bind)
    if "trades" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("trades")}
    with bind.begin() as conn:
        for name, sql_type in _ADDED_COLUMNS.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE trades ADD COLUMN {name} {sql_type}"))


def init_db() -> None:
    Base.metadata.create_all(engine)
    _migrate(engine)


@dataclass
class DailyTotals:
    """One day's realized result for one bot identity.

    Every number here comes from settled trades only. Floating profit, equity
    moves, deposits, manual trades and other magic numbers are all excluded by
    construction, because nothing but this bot's own settled rows is queried.
    """

    gross_profit: float = 0.0  # sum of the positive results
    gross_loss: float = 0.0  # absolute sum of the negative results
    net: float = 0.0  # gross_profit - gross_loss
    wins: int = 0
    losses: int = 0
    unsettled: int = 0  # closed trades whose realized result is not known yet


def daily_totals(symbol: str, mode: str, magic: int, trading_day: str) -> DailyTotals:
    """The shared daily accounting source.

    The engine's daily target and the dashboard's daily cards both read this, so
    they cannot disagree about what today made.
    """
    with SessionLocal() as session:
        rows = (
            session.query(TradeRecord)
            .filter(
                TradeRecord.symbol == symbol,
                TradeRecord.mode == mode,
                TradeRecord.magic == magic,
                TradeRecord.trading_day == trading_day,
                TradeRecord.status == "CLOSED",
            )
            .all()
        )
    totals = DailyTotals()
    for row in rows:
        if row.profit is None:
            # A trade whose result the broker has not reported yet. It is
            # counted as unsettled rather than as zero, because treating an
            # unknown as break-even could report a target as reached when it
            # was not, or the reverse.
            totals.unsettled += 1
            continue
        if row.profit > 0:
            totals.gross_profit += row.profit
            totals.wins += 1
        elif row.profit < 0:
            totals.gross_loss += abs(row.profit)
            totals.losses += 1
    totals.gross_profit = round(totals.gross_profit, 2)
    totals.gross_loss = round(totals.gross_loss, 2)
    totals.net = round(totals.gross_profit - totals.gross_loss, 2)
    return totals
