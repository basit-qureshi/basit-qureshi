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


# --- D8: the capital floor as an ACTIVE trigger, not only an entry rule -------
#
# Reviewer's reproduction: balance 1000, floor 950, basket stop 100, daily limit
# 200. Admission passed, a position took equity to 940, and a protective cycle
# left it open with no halt. The floor participated in admission and in nothing
# else, while the constructor described it as a line the account is never traded
# below. Those were two different contracts.


def under_the_floor(broker, engine_factory, **over):
    """A bot position that takes ACCOUNT EQUITY below the floor."""
    from app.brokers.base import OrderSide

    kw = dict(capital_floor_usd=950.0, basket_stop_loss_usd=100.0,
              max_daily_loss_usd=200.0, basket_take_profit_usd=10_000.0)
    kw.update(over)
    broker.balance = 1000.0
    e = engine_factory(**kw)
    e._protective_tick()
    broker.open_position(OrderSide.BUY, broker.price, magic=MAGIC)
    broker.price -= 60.0                      # one 0.01 lot -> about -60.00
    account = broker.get_account_info()
    assert account.equity < kw["capital_floor_usd"], "fixture: equity must breach the floor"
    return e


def test_a_floor_breach_closes_owned_exposure_and_latches(broker, engine_factory):
    e = under_the_floor(broker, engine_factory)
    e._protective_tick()

    assert e._halt_reason is not None and "capital floor" in e._halt_reason
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == [], (
        "the floor was breached and this bot's exposure stayed open"
    )
    assert e._entries_paused is True
    allowed, reason = e._entry_gate(broker.get_account_info())
    assert allowed is False and reason.startswith("HALTED"), reason


def test_a_floor_halt_is_written_down_before_the_close_is_attempted(broker, engine_factory,
                                                                   monkeypatch):
    """A crash mid-liquidation must not lose the reason."""
    from app import db as db_module

    e = under_the_floor(broker, engine_factory)
    monkeypatch.setattr(broker, "close_position",
                        lambda t: (_ for _ in ()).throw(RuntimeError("rejected")))
    e._protective_tick()

    saved = db_module.load_risk(e._risk_key()) or {}
    assert "capital floor" in (saved.get("halt_reason") or ""), (
        "the halt reason was not persisted before the close was attempted"
    )
    assert broker.get_open_positions("XAUUSD", magic=MAGIC), "fixture: the close must fail"


def test_a_floor_halt_keeps_trying_and_survives_a_restart(broker, engine_factory, monkeypatch):
    e = under_the_floor(broker, engine_factory)
    real_close = broker.close_position
    monkeypatch.setattr(broker, "close_position",
                        lambda t: (_ for _ in ()).throw(RuntimeError("rejected")))
    e._protective_tick()
    assert broker.get_open_positions("XAUUSD", magic=MAGIC)

    fresh = engine_factory(capital_floor_usd=950.0, basket_stop_loss_usd=100.0,
                           max_daily_loss_usd=200.0, basket_take_profit_usd=10_000.0)
    assert fresh._halt_reason is not None, "the floor halt did not survive the restart"
    monkeypatch.setattr(broker, "close_position", real_close)
    fresh._protective_tick()
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == [], (
        "the restarted engine did not finish the liquidation"
    )


def test_a_late_fill_after_a_floor_halt_is_closed_too(broker, engine_factory):
    from app.brokers.base import OrderSide

    e = under_the_floor(broker, engine_factory)
    e._protective_tick()
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == []

    broker.open_position(OrderSide.BUY, broker.price, magic=MAGIC)
    e._protective_tick()
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == [], (
        "a late fill survived a capital floor halt"
    )
    assert e._entries_paused is True


def test_a_floor_breach_never_touches_a_trade_this_bot_does_not_own(broker, engine_factory):
    """Equity is an ACCOUNT number; ownership still bounds what may be closed."""
    from app.brokers.base import OrderSide

    # A BUY, not a SELL: a manual SELL would GAIN as the price falls and net the
    # owned loss back to zero, and the fixture would not breach the floor at all.
    manual = broker.open_position(OrderSide.BUY, broker.price, magic=555777)
    e = under_the_floor(broker, engine_factory)
    e._protective_tick()

    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == []
    surviving = {p.ticket for p in broker.get_open_positions("XAUUSD")}
    assert manual.ticket in surviving, "the floor halt closed a manual trade"


def test_outside_losses_can_trigger_the_floor_and_the_reason_says_so(broker, engine_factory):
    """A manual position pushes equity under the floor.

    The bot cannot repair that: it removes its OWN exposure and stands down. The
    reason has to say that, or the owner will read a flat bot as a fixed account.
    """
    from app.brokers.base import OrderSide

    broker.balance = 1000.0
    e = engine_factory(capital_floor_usd=950.0, basket_stop_loss_usd=100.0,
                       max_daily_loss_usd=200.0, basket_take_profit_usd=10_000.0)
    e._protective_tick()
    broker.open_position(OrderSide.BUY, broker.price, magic=MAGIC)       # small, owned
    manual = broker.open_position(OrderSide.BUY, broker.price, magic=999111, volume=0.05)
    broker.price -= 20.0        # owned -20, manual -100 -> equity about 880

    account = broker.get_account_info()
    assert account.equity < 950.0
    e._protective_tick()

    assert e._halt_reason is not None and "capital floor" in e._halt_reason
    assert "does not own" in e._halt_reason, "the reason does not state the ownership limit"
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == []
    assert manual.ticket in {p.ticket for p in broker.get_open_positions("XAUUSD")}
    # The account is STILL under the floor, and the bot says so rather than
    # implying it fixed anything.
    assert broker.get_account_info().equity < 950.0


def test_an_unreadable_equity_is_reported_rather_than_silently_skipped(broker, engine_factory,
                                                                      monkeypatch):
    from app.brokers.base import OrderSide

    e = under_the_floor(broker, engine_factory)
    real = broker.get_account_info

    def no_equity(*a, **k):
        account = real(*a, **k)
        account.equity = None
        return account

    monkeypatch.setattr(broker, "get_account_info", no_equity)
    e._protective_tick()
    assert "equity is unreadable" in (e._last_error or ""), (
        "an unreadable equity silently skipped the floor check"
    )


def test_the_floor_with_no_exposure_refuses_entry_instead_of_halting(broker, engine_factory):
    """Nothing to liquidate means nothing to liquidate.

    An account below the floor with none of this bot's exposure open is refused
    by admission with a visible reason. Turning that into a persistent halt would
    lock a small account behind a manual reset for a breach its trading never
    caused.
    """
    broker.balance = 24.67
    e = engine_factory(basket_stop_loss_usd=10.0)     # fixture floor is 50.00
    e._tick()
    broker.next_candle()
    e._tick()

    assert e._halt_reason is None, "a halt was raised with nothing open"
    status = e.status()
    assert status["entry_blocked"] is True
    assert "capital floor" in (status["entry_block_reason"] or "")
    assert orders(broker) == []


# --- D9: decision values must not arrive pre-rounded --------------------------

class RawPosition:
    """A position whose net is deliberately just under a cent boundary."""

    def __init__(self, net: float, ticket: str = "raw-1"):
        self.profit, self.swap, self.commission = net, 0.0, 0.0
        self.volume, self.ticket, self.identifier = 0.01, ticket, None
        self.costs_known, self.side = True, None

    @property
    def net_profit(self) -> float:
        return self.profit + self.swap + self.commission


def test_the_target_is_judged_on_unrounded_money_through_the_real_caller(broker, engine_factory):
    """Reviewer's case: net 9.996 against a 10.00 target, zero exit reserve.

    The direct helper always answered this correctly. Its live caller handed it
    a value `_basket_pnl` had already rounded to 10.00, so the composition
    crossed a target the money had not. The regression goes through the
    composition, not the helper alone.
    """
    e = engine_factory(basket_take_profit_usd=10.0, basket_stop_loss_usd=60.0)
    positions = [RawPosition(9.996)]

    net, gross, known = e._basket_pnl(positions)
    assert net == 9.996, "the decision path received a rounded figure"
    assert e._profit_target_met(positions, net, known, 0.0) is False, (
        "9.996 was treated as having cleared a 10.00 target"
    )

    # And the display boundary still rounds.
    assert e._basket_pnl_display(positions)[0] == 10.0


def test_a_basket_that_genuinely_clears_the_target_still_closes(broker, engine_factory):
    e = engine_factory(basket_take_profit_usd=10.0, basket_stop_loss_usd=60.0)
    positions = [RawPosition(10.004)]
    net, _gross, known = e._basket_pnl(positions)
    assert e._profit_target_met(positions, net, known, 0.0) is True


def test_the_basket_stop_is_judged_on_unrounded_money_too(broker, engine_factory):
    """-59.996 against a 60.00 stop has not reached it."""
    e = engine_factory(basket_take_profit_usd=1000.0, basket_stop_loss_usd=60.0)
    net, gross, _known = e._basket_pnl([RawPosition(-59.996)])
    assert min(net, gross) == -59.996
    assert min(net, gross) > -e.basket_stop_loss_usd, (
        "a rounded reading would have fired the stop early"
    )


# --- D10: a missing day anchor that could never be repaired -------------------
#
# Seen on the owner's demo screen: the bot was running, nothing was open, and
# every cycle refused with "today's accounting is incomplete — no opening
# exposure anchor for this day". `_roll_day` establishes the anchor only when the
# day CHANGES, and `_ensure_day_bound` returned early once the day was bound, so
# nothing re-established it. The refusal could not clear until the next day.


def bound_day_without_anchor(engine, broker):
    """The stuck state: day bound, anchor missing."""
    engine._protective_tick()
    assert engine._trading_day is not None
    engine._day_open_marked = None          # as a failed read, or a restored null, leaves it
    return engine


def test_a_missing_anchor_blocks_entries(broker, engine_factory):
    """The symptom, before the repair runs."""
    e = engine_factory(basket_stop_loss_usd=60.0, max_daily_loss_usd=100.0)
    bound_day_without_anchor(e, broker)

    risk = e._day_risk([])
    assert risk.complete is False
    assert any("anchor" in reason for reason in risk.incomplete_reasons)


def test_the_anchor_is_repaired_when_this_bot_owns_nothing(broker, engine_factory):
    e = engine_factory(basket_stop_loss_usd=60.0, max_daily_loss_usd=100.0)
    bound_day_without_anchor(e, broker)
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == [], "fixture: nothing open"

    e._ensure_day_bound(broker.get_account_info())

    assert e._day_open_marked == 0.0, "the anchor was not repaired"
    assert e._day_risk([]).complete is True
    allowed, reason = e._entry_gate(broker.get_account_info())
    assert allowed is True, f"still blocked after the repair: {reason}"


def test_the_repair_survives_a_restart(broker, engine_factory):
    e = engine_factory(basket_stop_loss_usd=60.0, max_daily_loss_usd=100.0)
    bound_day_without_anchor(e, broker)
    e._ensure_day_bound(broker.get_account_info())

    fresh = engine_factory(basket_stop_loss_usd=60.0, max_daily_loss_usd=100.0)
    assert fresh._day_open_marked == 0.0, "the repaired anchor was not persisted"


def test_the_anchor_is_not_invented_while_something_is_open(broker, engine_factory):
    """With exposure open, what was carried into the day cannot be reconstructed.

    Setting it to zero here would subtract nothing and quietly count today's
    floating loss as if it had been carried in — hiding part of it from the
    daily limit. Blocking is the safe direction.
    """
    from app.brokers.base import OrderSide

    e = engine_factory(basket_stop_loss_usd=60.0, max_daily_loss_usd=100.0)
    bound_day_without_anchor(e, broker)
    broker.open_position(OrderSide.BUY, broker.price, magic=MAGIC)

    e._ensure_day_bound(broker.get_account_info())

    assert e._day_open_marked is None, "an anchor was invented over live exposure"
    assert e._day_risk().complete is False


def test_a_resting_order_also_stops_the_repair(broker, engine_factory):
    from app.brokers.base import PendingType

    e = engine_factory(basket_stop_loss_usd=60.0, max_daily_loss_usd=100.0)
    bound_day_without_anchor(e, broker)
    broker.place_pending_order("XAUUSD", PendingType.BUY_STOP, 0.01,
                               broker.price + 5.0, "GRID", MAGIC)

    e._ensure_day_bound(broker.get_account_info())
    assert e._day_open_marked is None, "an order that could fill was ignored"


def test_an_unreadable_broker_does_not_repair_the_anchor(broker, engine_factory, monkeypatch):
    e = engine_factory(basket_stop_loss_usd=60.0, max_daily_loss_usd=100.0)
    bound_day_without_anchor(e, broker)
    monkeypatch.setattr(broker, "get_open_positions",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("link down")))

    e._ensure_day_bound(broker.get_account_info())
    assert e._day_open_marked is None, "unreadable was treated as empty"


def test_an_existing_anchor_is_never_overwritten(broker, engine_factory):
    e = engine_factory(basket_stop_loss_usd=60.0, max_daily_loss_usd=100.0)
    e._protective_tick()
    e._day_open_marked = -12.34           # carried in from yesterday

    e._ensure_day_bound(broker.get_account_info())
    assert e._day_open_marked == -12.34, "a real anchor was replaced"


def test_a_running_engine_repairs_itself_on_the_next_cycle(broker, engine_factory):
    """End to end: the owner does nothing, and the next protective tick clears it."""
    e = engine_factory(basket_stop_loss_usd=60.0, max_daily_loss_usd=100.0)
    bound_day_without_anchor(e, broker)
    assert e._entry_gate(broker.get_account_info())[0] is False

    e._protective_tick()

    assert e._day_open_marked == 0.0
    allowed, reason = e._entry_gate(broker.get_account_info())
    assert allowed is True, f"the bot was still refusing after a full cycle: {reason}"
