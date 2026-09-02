"""The strategy rules that must not change. These are the guard rails."""

from app.brokers.base import OrderSide, PendingType
from tests.conftest import MAGIC


def orders(broker):
    return broker.get_pending_orders("XAUUSD", magic=MAGIC)


def armed(broker, engine):
    engine._tick()
    broker.next_candle()
    engine._tick()
    return orders(broker)


def test_grid_shape_lot_and_spacing(broker, engine_factory):
    e = engine_factory()
    placed = armed(broker, e)
    buys = sorted(o.price for o in placed if o.order_type == PendingType.BUY_STOP)
    sells = sorted((o.price for o in placed if o.order_type == PendingType.SELL_STOP), reverse=True)

    assert len(buys) == 10 and len(sells) == 10
    assert all(o.volume == 0.01 for o in placed), "every order must use the fixed lot"
    assert all(p > broker.price for p in buys)
    assert all(p < broker.price for p in sells)
    assert {round(buys[i + 1] - buys[i], 2) for i in range(9)} == {0.30}
    assert {round(sells[i] - sells[i + 1], 2) for i in range(9)} == {0.30}
    assert {o.comment for o in placed} == {"BUY GRID", "SELL GRID"}


def test_orders_are_isolated_by_magic_number(broker, engine_factory):
    e = engine_factory()
    armed(broker, e)
    assert broker.get_pending_orders("XAUUSD", magic=999) == []


def test_basket_closes_on_the_combined_total_not_a_single_trade(broker, engine_factory):
    e = engine_factory(basket_take_profit_usd=10.0)
    armed(broker, e)
    broker.price = 4004.0  # sweeps the buy grid; no single 0.01 lot is near $10
    broker.next_candle()
    e._tick()
    assert e._baskets_won == 1
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == []


def test_basket_holds_at_one_cent_short_and_closes_at_the_target(broker, engine_factory):
    e = engine_factory(basket_take_profit_usd=10.0)
    placed = armed(broker, e)
    # leave a single BUY STOP so the arithmetic is exact: 0.01 lots of gold
    # move $0.01 per point, so +$10 needs a $10 move
    keep = min((o for o in placed if o.order_type == PendingType.BUY_STOP), key=lambda o: o.price)
    for o in placed:
        if o.ticket != keep.ticket:
            broker.cancel_pending_order(o.ticket)
    broker.price = keep.price
    broker.next_candle()
    e._tick()
    position = broker.get_open_positions("XAUUSD", magic=MAGIC)[0]

    broker.price = position.open_price + 9.99
    broker.next_candle()
    e._tick()
    assert len(broker.get_open_positions("XAUUSD", magic=MAGIC)) == 1, "closed a cent short of the target"

    broker.price = position.open_price + 10.00
    broker.next_candle()
    e._tick()
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == []


def test_basket_stop_loss_closes_the_group(broker, engine_factory):
    e = engine_factory(basket_take_profit_usd=1000.0, basket_stop_loss_usd=5.0)
    armed(broker, e)
    broker.price = 4004.0   # fill the buy side
    broker.next_candle()
    e._tick()
    broker.price = 3990.0   # then collapse
    broker.next_candle()
    e._tick()
    assert e._baskets_stopped == 1
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == []


def test_max_open_positions_pulls_the_rest_of_the_grid(broker, engine_factory):
    e = engine_factory(max_open_positions=4, basket_take_profit_usd=1000.0)
    armed(broker, e)
    broker.price = 4004.0
    broker.next_candle()
    e._tick()
    e._tick()
    assert orders(broker) == [], "orders kept resting past the position cap"


def test_risk_halt_flattens_and_stands_down(broker, engine_factory):
    e = engine_factory(basket_take_profit_usd=1000.0, basket_stop_loss_usd=6.0, max_daily_loss_usd=5.0)
    armed(broker, e)
    broker.price = 4004.0
    broker.next_candle()
    e._tick()
    broker.price = 3985.0
    broker.next_candle()
    e._tick()          # basket stop books the loss
    broker.next_candle()
    e._tick()          # the daily loss limit then halts
    assert e._halt_reason is not None
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == []
    assert orders(broker) == []


def test_a_fully_hedged_basket_is_detected(broker, engine_factory):
    e = engine_factory()
    broker.open_position("BUY", 4001.0)
    broker.open_position("SELL", 3999.0)
    positions = broker.get_open_positions("XAUUSD", magic=MAGIC)
    assert e._is_hedged(positions) is True
    broker.open_position("BUY", 4002.0)
    assert e._is_hedged(broker.get_open_positions("XAUUSD", magic=MAGIC)) is False


def test_manual_trades_are_never_touched(broker, engine_factory):
    foreign = broker.open_position("BUY", 3995.0, magic=12345, volume=0.05)
    e = engine_factory(basket_take_profit_usd=10.0)
    armed(broker, e)
    broker.price = 4004.0
    broker.next_candle()
    e._tick()
    assert foreign.ticket in broker.positions, "a trade from another magic number was closed"
    assert broker.positions[foreign.ticket].volume == 0.05


def test_grid_positions_carry_no_individual_stop_or_target(broker, engine_factory):
    e = engine_factory(basket_take_profit_usd=1000.0)
    armed(broker, e)
    broker.price = 4002.0
    broker.next_candle()
    e._tick()
    positions = broker.get_open_positions("XAUUSD", magic=MAGIC)
    assert positions
    assert all(p.sl == 0 and p.tp == 0 for p in positions)
