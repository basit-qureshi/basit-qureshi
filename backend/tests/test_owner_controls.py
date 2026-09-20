"""The controls the owner operates: the trading window, the entry block and
releasing a halt."""

from tests.conftest import MAGIC


def orders(broker):
    return broker.get_pending_orders("XAUUSD", magic=MAGIC)


def test_the_trading_window_is_read_in_the_owners_timezone(broker, engine_factory, monkeypatch):
    """The hours are set on a Pakistan clock. Reading them as UTC shifted every
    window by five hours, so "on at 17:00" actually meant 22:00 PKT."""
    from datetime import datetime, timezone
    import app.engine.grid_engine as ge

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            # 13:00 UTC is 18:00 in Karachi.
            return datetime(2026, 1, 5, 13, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(ge, "datetime", FixedDatetime)

    inside = engine_factory(trading_start_hour=17, trading_end_hour=22)
    assert inside._within_session() is True, "18:00 PKT should be inside a 17:00-22:00 PKT window"

    outside = engine_factory(trading_start_hour=9, trading_end_hour=13)
    assert outside._within_session() is False, "18:00 PKT should be outside a 09:00-13:00 PKT window"


def test_the_window_is_reported_in_the_owners_clock(engine_factory):
    e = engine_factory(trading_start_hour=17, trading_end_hour=22)
    assert e.session_label() == "17:00-22:00 Karachi"
    assert engine_factory().session_label() == "always on"


def test_no_orders_are_placed_outside_the_window(broker, engine_factory, monkeypatch):
    from datetime import datetime, timezone
    import app.engine.grid_engine as ge

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 5, 13, 0, tzinfo=timezone.utc)  # 18:00 PKT

    monkeypatch.setattr(ge, "datetime", FixedDatetime)
    e = engine_factory(trading_start_hour=1, trading_end_hour=5, basket_stop_loss_usd=60.0)
    e._tick()
    broker.next_candle()
    e._tick()
    assert orders(broker) == []


def test_status_explains_why_entries_are_refused(broker, engine_factory):
    """A refusal the owner cannot see is a bot that looks broken. The reason has
    to reach the dashboard."""
    broker.balance = 24.67
    e = engine_factory(basket_stop_loss_usd=10.0)
    e._tick()
    broker.next_candle()
    e._tick()

    status = e.status()
    assert status["entry_blocked"] is True
    assert "NO_TRADE" in status["entry_block_reason"]
    assert status["trading_window"] == "always on"


def test_a_halt_cannot_be_cleared_while_exposure_is_open(broker, engine_factory):
    e = engine_factory(basket_take_profit_usd=1000.0, max_daily_loss_usd=5.0, basket_stop_loss_usd=60.0)
    e._tick()
    broker.next_candle()
    e._tick()
    broker.open_position("BUY", 4000.0)   # something this bot owns is still live
    e._halt_reason = "daily loss limit reached (-6.00 of -5.00)"

    ok, message = e.clear_halt()
    assert ok is False
    assert "still open" in message
    assert e._halt_reason is not None


def test_a_halt_clears_once_the_account_is_flat(broker, engine_factory):
    e = engine_factory(basket_take_profit_usd=1000.0, max_daily_loss_usd=5.0, basket_stop_loss_usd=60.0)
    e._halt_reason = "daily loss limit reached (-6.00 of -5.00)"
    e._persist_risk_state()

    ok, message = e.clear_halt()
    assert ok is True, message
    assert e._halt_reason is None

    # and it stays cleared for the next engine that reads the same record
    fresh = engine_factory(basket_take_profit_usd=1000.0, max_daily_loss_usd=5.0, basket_stop_loss_usd=60.0)
    assert fresh._halt_reason is None


def test_the_drawdown_high_water_mark_is_not_reset_by_a_new_day(broker, engine_factory):
    """Resetting the peak every morning would let an account bleed down
    indefinitely, one 'fresh' day at a time."""
    e = engine_factory(basket_stop_loss_usd=60.0)
    e._tick()
    broker.balance = 1200.0          # a new high
    broker.next_candle()
    e._tick()
    peak = e._equity_peak
    assert peak >= 1200.0

    broker.balance = 900.0
    broker.next_candle(minutes=60 * 24)   # a new broker day, at a lower equity
    e._tick()
    assert e._equity_peak == peak, "the high-water mark was reset by the day rollover"
