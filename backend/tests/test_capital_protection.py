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
    # 40.00, not 5.00: a limit below the ~$37.80 a completed grid freezes at is
    # refused at admission now, and this test is about the limit FIRING.
    e = engine_factory(basket_take_profit_usd=1000.0, max_daily_loss_usd=40.0)
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
    # Pull the sell side first. Left resting it fills on the way down and the
    # basket freezes at the ~$37.80 a completed grid locks in - which admission
    # now guarantees is INSIDE the daily budget, so a hedged basket can no
    # longer reach the limit on its own. This test is about the limit firing,
    # so the loss is kept directional.
    for o in list(orders(broker)):
        broker.cancel_pending_order(o.ticket)
    # A real marked loss, not a poke at an internal counter: the daily limit is
    # now judged on settled trades plus the change in open mark, so moving the
    # price is what puts the day past its limit.
    broker.price -= 12.0         # the filled buy side is now well under water

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
    e = engine_factory(basket_take_profit_usd=1000.0, max_daily_loss_usd=40.0)
    armed(broker, e)
    broker.price += 4.0          # fill the buy side
    broker.next_candle()
    e._tick()
    for o in list(orders(broker)):   # keep the loss directional, not frozen
        broker.cancel_pending_order(o.ticket)
    broker.price -= 12.0         # and drive the day past its limit on the mark
    broker.next_candle()
    e._tick()
    assert e._halt_reason is not None

    fresh = engine_factory(basket_take_profit_usd=1000.0, max_daily_loss_usd=40.0)
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


# --- D6: a grid that can only end by forcing a limit -------------------------
#
# Admission already refuses a grid whose completed-grid freeze is larger than
# the BASKET budget ("the grid as configured cannot finish building without
# breaching it"). The same sentence is true of the other two limits the owner
# set, and nothing used to check them.


def settled_loss(engine, amount: float) -> None:
    """Puts a real settled loss on today's books for this engine's account."""
    from app import db as db_module
    from app.db import TradeRecord

    with db_module.SessionLocal() as session:
        session.add(TradeRecord(
            account_id=engine._account_id, ticket=f"settled-{amount}", symbol="XAUUSD",
            side="BUY", volume=0.01, open_price=4000.0, sl=0, tp=0, profit=amount,
            mode="demo", status="CLOSED", magic=MAGIC, trading_day=engine._trading_day,
        ))
        session.commit()
    engine._refresh_daily_totals()


def test_a_grid_that_cannot_fit_the_remaining_daily_budget_is_refused(broker, engine_factory):
    """The day has $15 left and a completed grid freezes at about $37.80.

    Placing it means the basket's only possible ending is the daily limit
    liquidating it, and a fast oscillation that fills both sides between two
    protective cycles lands the day well past the limit before anything can
    fire. The limit can only be honoured to within one tick's movement, so the
    known freeze has to fit inside what is left of the budget.
    """
    e = engine_factory(basket_stop_loss_usd=60.0, max_daily_loss_usd=100.0)
    e._protective_tick()                       # binds the account and the day
    settled_loss(e, -85.0)                     # $15 of the budget left

    allowed, reason = e._entry_gate(broker.get_account_info())
    assert allowed is False
    assert "loss budget" in reason and "37.80" in reason
    assert orders(broker) == []


def test_the_same_grid_is_allowed_while_the_budget_can_absorb_it(broker, engine_factory):
    """The guard must bite only when it should: an untouched budget still trades."""
    e = engine_factory(basket_stop_loss_usd=60.0, max_daily_loss_usd=100.0)
    e._protective_tick()
    settled_loss(e, -40.0)                     # $60 left, comfortably over the freeze

    allowed, reason = e._entry_gate(broker.get_account_info())
    assert allowed is True, reason


def test_a_grid_that_would_break_the_capital_floor_is_refused(broker, engine_factory):
    """The floor is the balance the account is not traded down past."""
    broker.balance = 500.0
    e = engine_factory(basket_stop_loss_usd=60.0, max_daily_loss_usd=1000.0,
                       capital_floor_usd=470.0)         # $30 of headroom
    e._protective_tick()

    allowed, reason = e._entry_gate(broker.get_account_info())
    assert allowed is False
    assert "capital floor" in reason
    assert orders(broker) == []


# --- D7: a halted engine ignoring exposure that appears after its own close --
def test_a_completed_close_releases_its_intent(broker, engine_factory):
    e = engine_factory(basket_take_profit_usd=1000.0, max_daily_loss_usd=40.0)
    armed(broker, e)
    broker.price += 4.0
    broker.next_candle()
    e._tick()
    for o in list(orders(broker)):
        broker.cancel_pending_order(o.ticket)
    broker.price -= 12.0
    broker.next_candle()
    e._tick()

    assert e._halt_reason is not None
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == []
    assert e._close_intent is None, (
        "a confirmed close left its intent attached; the re-open branch in "
        "_check_risk_limits only fires when no intent is attached"
    )


def test_a_halted_engine_still_closes_exposure_that_appears_afterwards(broker, engine_factory):
    """The state the bot must never ignore exposure in.

    A cancel that is acknowledged while a fill is already in flight leaves a
    position behind after the close confirmed flat. While a DONE intent stayed
    attached, the halt path skipped re-opening a close and the position sat
    there for as long as the halt lasted.
    """
    from app.brokers.base import OrderSide

    e = engine_factory(basket_take_profit_usd=1000.0, max_daily_loss_usd=40.0)
    armed(broker, e)
    broker.price += 4.0
    broker.next_candle()
    e._tick()
    for o in list(orders(broker)):
        broker.cancel_pending_order(o.ticket)
    broker.price -= 12.0
    broker.next_candle()
    e._tick()
    assert e._halt_reason is not None and broker.get_open_positions("XAUUSD", magic=MAGIC) == []

    # A real entry price, not a placeholder: a position opened at 0.01 would
    # carry an absurd mark and the test would pass for the wrong reason.
    broker.open_position(OrderSide.BUY, broker.price, magic=MAGIC)   # the late fill
    assert broker.get_open_positions("XAUUSD", magic=MAGIC), "fixture: exposure must exist"

    e._protective_tick()
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == [], (
        "a halted engine left exposure it owns open at the broker"
    )


# --- D6b: the boundary of the budget comparison -------------------------------
#
# The rule is `estimate > remaining` refuses. Both sides of that boundary are
# pinned, because a guard whose edge nobody tested is a guard that drifts.


def remaining_for(engine, broker, target: float) -> float:
    """Books a settled loss that leaves exactly `target` of the daily budget."""
    from app.engine import grid_math

    est = grid_math.completed_grid_estimate(
        broker.get_current_price("XAUUSD"),
        grid_math.GridSpec(buy_levels=engine.buy_stop_levels, sell_levels=engine.sell_stop_levels,
                           lot=engine.lot_size, distance=engine.grid_distance),
        grid_math.SymbolSpec.from_broker(broker.get_symbol_info("XAUUSD"))).total
    settled_loss(engine, round(target - engine.max_daily_loss_usd, 2))
    return est


def test_an_estimate_exactly_equal_to_the_remaining_budget_is_allowed(broker, engine_factory):
    e = engine_factory(basket_stop_loss_usd=60.0, max_daily_loss_usd=100.0)
    e._protective_tick()
    estimate = remaining_for(e, broker, 37.80)
    assert estimate == 37.80, "fixture drifted; the boundary below is no longer the boundary"

    allowed, reason = e._entry_gate(broker.get_account_info())
    assert allowed is True, f"equal must fit, the rule refuses only when larger: {reason}"


def test_one_cent_less_budget_than_the_estimate_is_refused(broker, engine_factory):
    e = engine_factory(basket_stop_loss_usd=60.0, max_daily_loss_usd=100.0)
    e._protective_tick()
    remaining_for(e, broker, 37.79)

    allowed, reason = e._entry_gate(broker.get_account_info())
    assert allowed is False
    assert "37.79" in reason and "37.80" in reason, reason


def test_the_refusal_states_a_policy_rather_than_predicting_the_outcome(broker, engine_factory):
    """A refused basket might have reached its target. The reason must not claim
    otherwise — the ground is the declared policy, not a forecast."""
    e = engine_factory(basket_stop_loss_usd=60.0, max_daily_loss_usd=100.0)
    e._protective_tick()
    remaining_for(e, broker, 15.0)
    _, reason = e._entry_gate(broker.get_account_info())

    assert "policy" in reason.lower()
    for forecast in ("could only end", "will be liquidated", "only way"):
        assert forecast not in reason.lower(), f"the refusal predicts the future: {reason}"
    assert "excludes exit costs" in reason, "the refusal must not imply a maximum loss"


def test_protection_over_preexisting_exposure_needs_no_admission(broker, engine_factory):
    """Constructed directly, because admission would refuse to create this state.

    The engine has to protect exposure it owns whether or not today's admission
    policy would have allowed it: settings change, and a basket opened under an
    earlier configuration is still the bot's to manage.
    """
    from app.brokers.base import OrderSide

    # A daily budget far too small for this grid — admission would never place it.
    e = engine_factory(basket_take_profit_usd=1000.0, basket_stop_loss_usd=25.0,
                       max_daily_loss_usd=5.0)
    e._protective_tick()                       # bind the account and the day
    for _ in range(3):
        broker.open_position(OrderSide.BUY, broker.price, magic=MAGIC)
    assert e._entry_gate(broker.get_account_info())[0] is False, (
        "fixture: this configuration must be one admission refuses"
    )

    broker.price -= 20.0                       # the owned exposure goes under water
    e._protective_tick()

    assert e._halt_reason is not None, "owned exposure was not protected"
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == [], (
        "the engine refused to manage exposure it already owned"
    )
