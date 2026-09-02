"""The strict next-M1-candle gate, for every event that arms it."""

from app.brokers.base import PendingType
from tests.conftest import MAGIC


def orders(broker):
    return broker.get_pending_orders("XAUUSD", magic=MAGIC)


# 1 + 2 -------------------------------------------------------------------
def test_start_places_nothing_on_the_activation_candle(broker, engine_factory):
    e = engine_factory()
    e._tick()
    assert orders(broker) == [], "a grid was placed on the very candle the bot became ready"


def test_start_places_the_grid_on_the_next_candle(broker, engine_factory):
    e = engine_factory()
    e._tick()
    assert orders(broker) == []
    broker.next_candle()
    e._tick()
    placed = orders(broker)
    assert len(placed) == 20
    assert sum(1 for o in placed if o.order_type == PendingType.BUY_STOP) == 10
    assert sum(1 for o in placed if o.order_type == PendingType.SELL_STOP) == 10


def test_repeated_polls_during_the_activation_candle_place_nothing(broker, engine_factory):
    e = engine_factory()
    for _ in range(5):
        e._tick()
    assert orders(broker) == []


# 3 -----------------------------------------------------------------------
def test_repeated_polls_after_the_new_candle_do_not_duplicate(broker, engine_factory):
    e = engine_factory()
    e._tick()
    broker.next_candle()
    for _ in range(5):
        e._tick()
    assert len(orders(broker)) == 20


# 4 -----------------------------------------------------------------------
def test_basket_close_waits_a_candle_before_rebuilding(broker, engine_factory):
    e = engine_factory(basket_take_profit_usd=10.0)
    e._tick()
    broker.next_candle()
    e._tick()
    assert len(orders(broker)) == 20

    broker.price = 4004.0  # sweeps the buy side and pushes the basket past +$10
    broker.next_candle()
    e._tick()
    assert e._baskets_won == 1
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == []
    assert orders(broker) == [], "rebuilt on the candle the profit was booked on"

    e._tick()
    assert orders(broker) == [], "rebuilt while the same candle was still running"

    broker.next_candle()
    e._tick()
    assert len(orders(broker)) == 20


# 5 -----------------------------------------------------------------------
def test_manual_removal_of_all_orders_waits_a_candle(broker, engine_factory):
    e = engine_factory()
    e._tick()
    broker.next_candle()
    e._tick()
    assert len(orders(broker)) == 20

    for o in list(orders(broker)):  # deleted by hand in MT5
        broker.cancel_pending_order(o.ticket)
    e._tick()
    assert orders(broker) == [], "rebuilt on the same candle the orders were removed on"
    e._tick()
    assert orders(broker) == []

    broker.next_candle()
    e._tick()
    assert len(orders(broker)) == 20


# 6 -----------------------------------------------------------------------
def test_partial_manual_deletion_is_not_topped_up(broker, engine_factory):
    e = engine_factory()
    e._tick()
    broker.next_candle()
    e._tick()
    for o in list(orders(broker))[:5]:
        broker.cancel_pending_order(o.ticket)
    broker.next_candle()
    e._tick()
    assert len(orders(broker)) == 15, "a partially deleted grid was topped back up"


# 7 -----------------------------------------------------------------------
def test_unreadable_candles_place_nothing(broker, engine_factory):
    e = engine_factory()
    broker.candles_available = False
    for _ in range(3):
        e._tick()
    assert orders(broker) == []
    # and it recovers once data returns, still on a later candle
    broker.candles_available = True
    e._tick()
    assert orders(broker) == []
    broker.next_candle()
    e._tick()
    assert len(orders(broker)) == 20


# 8 -----------------------------------------------------------------------
def test_start_does_not_disturb_existing_bot_state(broker, engine_factory):
    e = engine_factory()
    e._tick()
    broker.next_candle()
    e._tick()
    existing = {o.ticket for o in orders(broker)}
    assert len(existing) == 20

    e2 = engine_factory()  # a restart with the same identity
    assert e2._own_state_exists()
    e2._tick()
    assert {o.ticket for o in orders(broker)} == existing, "an existing grid was replaced by a restart"


def test_start_with_an_open_position_keeps_managing_it(broker, engine_factory):
    broker.open_position("BUY", 3999.0)
    e = engine_factory()
    assert e._own_state_exists()
    e._tick()
    assert len(broker.get_open_positions("XAUUSD", magic=MAGIC)) == 1
    assert orders(broker) == [], "a new grid was stacked on top of an existing position"
