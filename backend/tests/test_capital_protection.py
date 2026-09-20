"""Stage A: capital protection defects, reproduced before they are fixed.

Each test here names a way the bot can lose more than the owner allowed, or
carry exposure nobody is watching. They are written against the engine's own
interface with an injected broker — no real orders, ever.
"""

import pytest

from tests.conftest import MAGIC


def orders(broker):
    return broker.get_pending_orders("XAUUSD", magic=MAGIC)


def armed(broker, engine):
    """Runs the engine to the point where it holds a live grid."""
    engine._tick()
    broker.next_candle()
    engine._tick()
    return orders(broker)


# --- D1: the basket is judged on gross profit, not net -----------------------
def test_basket_target_waits_for_net_profit_not_gross(broker, engine_factory):
    """Swap and commission are real money. A basket whose gross profit reaches
    the target but whose net does not has not earned the target, and closing it
    there books less than the owner asked for."""
    e = engine_factory(basket_take_profit_usd=10.0)
    armed(broker, e)

    # One position: gross +$10.40, but $1.00 of costs leaves +$9.40 net.
    for o in list(orders(broker)):
        broker.cancel_pending_order(o.ticket)
    pos = broker.open_position("BUY", broker.price - 10.40)
    pos.commission = -0.70
    pos.swap = -0.30

    broker.next_candle()
    e._tick()

    remaining = broker.get_open_positions("XAUUSD", magic=MAGIC)
    assert remaining, (
        "closed on gross profit: the basket was worth +10.40 gross but only +9.40 "
        "after costs, which is below the 10.00 target"
    )


def test_basket_stop_measures_net_loss(broker, engine_factory):
    """The same in the other direction: costs make a loss worse, so a stop that
    reads gross lets the account lose more than the configured amount."""
    e = engine_factory(basket_take_profit_usd=1000.0, basket_stop_loss_usd=10.0)
    armed(broker, e)
    for o in list(orders(broker)):
        broker.cancel_pending_order(o.ticket)

    pos = broker.open_position("BUY", broker.price + 9.50)  # gross -9.50
    pos.commission = -0.70
    pos.swap = -0.30                                        # net -10.50

    broker.next_candle()
    e._tick()

    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == [], (
        "the net loss was 10.50 against a 10.00 stop and the basket stayed open"
    )


# --- D2: a failed risk close is never retried --------------------------------
def test_a_failed_risk_close_is_retried_until_flat(broker, engine_factory, monkeypatch):
    """The dangerous case. The limit fires, the close fails, and on every later
    poll the bot reports itself halted while the positions are still live."""
    e = engine_factory(basket_take_profit_usd=1000.0, max_daily_loss_usd=5.0)
    armed(broker, e)

    broker.price += 4.0          # fill the buy side
    broker.next_candle()
    e._tick()
    assert broker.get_open_positions("XAUUSD", magic=MAGIC)

    real_close = broker.close_position
    broker_ok = {"value": False}

    def flaky_close(ticket):
        if not broker_ok["value"]:
            raise RuntimeError("broker rejected the close")
        return real_close(ticket)

    monkeypatch.setattr(broker, "close_position", flaky_close)
    e._day_realized = -6.0       # past the 5.00 daily loss limit

    broker.next_candle()
    e._tick()                    # halts, tries to close, every close fails
    assert e._halt_reason is not None
    assert broker.get_open_positions("XAUUSD", magic=MAGIC), "the failing close should have left exposure"

    for _ in range(3):           # the bot must keep trying, not give up
        broker.next_candle()
        e._tick()
    assert broker.get_open_positions("XAUUSD", magic=MAGIC), "fixture broke: closes were supposed to fail"

    broker_ok["value"] = True    # the broker recovers
    broker.next_candle()
    e._tick()

    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == [], (
        "the bot stayed halted with live positions and never retried the close"
    )


# --- D3: the halt does not survive a restart ---------------------------------
def test_a_risk_halt_survives_a_restart(broker, engine_factory):
    """Pressing Start, or restarting the backend, must not clear a loss halt.
    Otherwise the protection lasts exactly as long as the process does."""
    e = engine_factory(basket_take_profit_usd=1000.0, max_daily_loss_usd=5.0)
    armed(broker, e)
    e._day_realized = -6.0
    broker.next_candle()
    e._tick()
    assert e._halt_reason is not None

    fresh = engine_factory(basket_take_profit_usd=1000.0, max_daily_loss_usd=5.0)
    for _ in range(3):
        broker.next_candle()
        fresh._tick()

    assert orders(broker) == [], "a restarted engine placed a grid while a loss halt was unresolved"
    assert fresh._halt_reason is not None, "the loss halt was forgotten by the new engine"


# --- D4: the grid is placed without checking it is affordable ----------------
def test_an_unaffordable_grid_is_refused(broker, engine_factory):
    """A 10+10 grid at 0.01 lots is 0.20 lots of gold: roughly $20 of profit or
    loss per $1 of price. On a $24.67 account a $2 move is most of the account.
    The bot must refuse rather than place it."""
    broker.balance = 24.67
    e = engine_factory(basket_stop_loss_usd=10.0)

    e._tick()
    broker.next_candle()
    e._tick()

    assert orders(broker) == [], (
        "placed 0.20 lots of gold exposure against a 24.67 account without checking "
        "it could be afforded"
    )


def test_an_affordable_grid_is_still_placed(broker, engine_factory):
    """The guard must not simply block everything — a funded account still trades."""
    broker.balance = 5000.0
    e = engine_factory(basket_stop_loss_usd=60.0)
    assert len(armed(broker, e)) == 20


# --- D5: entries are allowed before the owner has set any limits -------------
def test_entries_are_blocked_until_loss_limits_are_configured(broker, engine_factory):
    """With every loss limit switched off there is nothing between a losing
    basket and the account. That state must not be allowed to trade."""
    e = engine_factory(
        basket_stop_loss_usd=0.0,
        max_daily_loss_usd=0.0,
        max_equity_drawdown_percent=0.0,
    )
    e._tick()
    broker.next_candle()
    e._tick()
    assert orders(broker) == [], "traded with no basket stop, no daily loss limit and no drawdown limit"
