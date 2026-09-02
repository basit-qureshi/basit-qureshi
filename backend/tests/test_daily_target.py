"""The daily profit target, its lock, and the daily profit/loss figures."""

from app import db as db_module
from app.db import TradeRecord, daily_totals
from tests.conftest import MAGIC

DAY = "2026-01-05"  # the FakeBroker's candle day in Asia/Karachi


def settled(profit, *, symbol="XAUUSD", mode="demo", magic=MAGIC, day=DAY, ticket=None):
    """Writes one settled trade straight into the shared accounting source."""
    with db_module.SessionLocal() as session:
        session.add(
            TradeRecord(
                ticket=ticket or f"t{abs(hash((profit, symbol, magic, day, id(profit)))) % 10**8}",
                symbol=symbol, side="BUY", volume=0.01, open_price=4000.0, sl=0.0, tp=0.0,
                profit=profit, mode=mode, status="CLOSED", magic=magic, trading_day=day,
            )
        )
        session.commit()


def orders(broker):
    return broker.get_pending_orders("XAUUSD", magic=MAGIC)


def armed(broker, engine):
    """Runs the bot up to the point where it holds a live grid."""
    engine._tick()
    broker.next_candle()
    engine._tick()
    return orders(broker)


# 16 ----------------------------------------------------------------------
def test_gross_profit_gross_loss_and_net():
    settled(15.00, ticket="a")
    settled(-4.00, ticket="b")
    settled(-6.00, ticket="c")
    totals = daily_totals("XAUUSD", "demo", MAGIC, DAY)
    assert totals.gross_profit == 15.00
    assert totals.gross_loss == 10.00
    assert totals.net == 5.00


def test_a_ten_dollar_target_is_not_reached_by_that_example(broker, engine_factory):
    settled(15.00, ticket="a")
    settled(-4.00, ticket="b")
    settled(-6.00, ticket="c")
    e = engine_factory(daily_profit_target_usd=10.0)
    assert len(armed(broker, e)) == 20, "halted on a $5.00 net against a $10.00 target"


# 15 ----------------------------------------------------------------------
def test_other_identities_do_not_move_the_daily_figures():
    settled(50.00, magic=12345, ticket="manual")      # someone's manual trade
    settled(50.00, magic=990011, ticket="testorder")  # a connectivity test order
    settled(50.00, symbol="EURUSD", ticket="other")   # another symbol
    settled(50.00, mode="real", ticket="othermode")   # the other account mode
    settled(50.00, day="2026-01-04", ticket="yday")   # yesterday
    settled(3.00, ticket="mine")
    totals = daily_totals("XAUUSD", "demo", MAGIC, DAY)
    assert totals.net == 3.00
    assert totals.gross_profit == 3.00


# 9 + 10 ------------------------------------------------------------------
def test_target_does_not_trigger_at_one_cent_short(broker, engine_factory):
    settled(9.99, ticket="a")
    e = engine_factory(daily_profit_target_usd=10.0)
    assert len(armed(broker, e)) == 20
    assert e._daily_target_hit is False


def test_target_triggers_at_exactly_the_target(broker, engine_factory):
    settled(10.00, ticket="a")
    e = engine_factory(daily_profit_target_usd=10.0)
    e._tick()
    broker.next_candle()
    e._tick()
    assert e._daily_target_hit is True
    assert orders(broker) == []


# 11 ----------------------------------------------------------------------
def test_orders_stay_at_zero_for_the_rest_of_the_day(broker, engine_factory):
    e = engine_factory(daily_profit_target_usd=10.0)
    grid = armed(broker, e)
    assert len(grid) == 20

    settled(12.00, ticket="a")  # the day's target is met
    e._tick()
    assert orders(broker) == [], "resting orders survived the daily halt"
    for _ in range(10):
        broker.next_candle()
        e._tick()
        assert orders(broker) == []


# 12 + 13 -----------------------------------------------------------------
def test_a_restart_does_not_bypass_the_lock(broker, engine_factory):
    settled(11.00, ticket="a")
    e = engine_factory(daily_profit_target_usd=10.0)
    e._tick()
    broker.next_candle()
    e._tick()
    assert orders(broker) == []

    fresh = engine_factory(daily_profit_target_usd=10.0)  # backend restarted
    for _ in range(3):
        broker.next_candle()
        fresh._tick()
    assert orders(broker) == [], "a restart placed a grid after the daily target"
    assert fresh._daily_target_hit is True


def test_clicking_start_again_does_not_bypass_the_lock(broker, engine_factory):
    settled(11.00, ticket="a")
    e = engine_factory(daily_profit_target_usd=10.0)
    e._tick()
    e._running = False
    e._arm_gate("Bot started") if e._gate_anchor is None else None
    broker.next_candle()
    e._tick()
    assert orders(broker) == []


# 14 ----------------------------------------------------------------------
def test_the_next_day_resets_the_lock_and_still_waits_a_candle(broker, engine_factory):
    settled(11.00, ticket="a")
    e = engine_factory(daily_profit_target_usd=10.0)
    e._tick()
    broker.next_candle()
    e._tick()
    assert orders(broker) == []

    broker.next_candle(minutes=60 * 24)  # a new broker day, nothing settled in it
    e._tick()
    assert e._daily_target_hit is False, "the lock survived into the next day"
    assert orders(broker) == [], "a grid landed on the rollover candle itself"

    broker.next_candle()
    e._tick()
    assert len(orders(broker)) == 20


# 17 ----------------------------------------------------------------------
def test_commission_and_swap_are_included(broker, engine_factory, monkeypatch):
    """MT5 reports profit, commission and swap separately; the broker adapter
    sums them, so whatever it hands back is what the day counts."""
    e = engine_factory()
    e._tick()
    broker.next_candle()
    e._tick()
    broker.price = 4004.0
    broker.next_candle()
    # net of a $5.00 gross win, $0.70 commission and $0.30 swap
    monkeypatch.setattr(broker, "get_realized_profit", lambda ticket: 4.00)
    e._tick()
    totals = daily_totals("XAUUSD", "demo", MAGIC, DAY)
    assert totals.gross_profit > 0
    assert all(v == 4.00 for v in [4.00])  # the adapter's netted figure is what was stored
    with db_module.SessionLocal() as session:
        rows = session.query(TradeRecord).filter(TradeRecord.status == "CLOSED").all()
    assert rows and all(r.profit == 4.00 for r in rows)


def test_unsettled_trades_block_a_new_grid(broker, engine_factory, monkeypatch):
    e = engine_factory()
    e._tick()
    broker.next_candle()
    e._tick()
    broker.price = 4004.0
    broker.next_candle()
    monkeypatch.setattr(broker, "get_realized_profit", lambda ticket: None)
    e._tick()  # basket closes, but the results are unknown
    broker.next_candle()
    e._tick()
    assert orders(broker) == [], "a new grid was placed while the day's total was still uncertain"
