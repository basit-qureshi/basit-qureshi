from datetime import datetime, timezone

from sqlalchemy import create_engine, String, Float, DateTime, Integer
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


engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)
