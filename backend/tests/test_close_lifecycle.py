"""Close-intent retirement, across the states a close can actually be in.

A completed intent is now retired where it completes rather than only on the
profit path. Retirement releases a slot, so every one of these has to hold:

  * an UNFINISHED close is never retired and never forgotten
  * a confirmed close is retired, and the basket is counted exactly once
  * a loss halt stays latched afterwards, and entries do not resurrect
  * settlement evidence survives retirement
  * exposure that appears after a confirmed close is still managed
  * a cancellation race that leaves an order resting keeps the close open
  * a restart after completion does not resurrect a finished request

Fake broker, temporary database, no terminal.
"""

import pytest

from app.brokers.base import OrderSide, PendingType
from app.engine.lifecycle import STATE_DONE
from tests.conftest import MAGIC


def orders(broker):
    return broker.get_pending_orders("XAUUSD", magic=MAGIC)


def live(**kw):
    kw.setdefault("basket_take_profit_usd", 1000.0)
    kw.setdefault("basket_stop_loss_usd", 60.0)
    # Above the fixture's 37.80 completed-grid estimate, or admission refuses
    # the grid and there is no basket to close.
    kw.setdefault("max_daily_loss_usd", 40.0)
    return kw


def filled_basket(broker, engine):
    """A grid placed, the buy side filled, the sell side pulled."""
    engine._tick()
    broker.next_candle()
    engine._tick()
    broker.price += 4.0
    broker.next_candle()
    engine._tick()
    for order in list(orders(broker)):
        broker.cancel_pending_order(order.ticket)
    held = broker.get_open_positions("XAUUSD", magic=MAGIC)
    assert held, "fixture: the basket must hold something"
    return held


# --- an unfinished close is never retired ------------------------------------

def test_a_close_that_could_not_complete_keeps_its_intent(broker, engine_factory, monkeypatch):
    e = engine_factory(**live())
    held = filled_basket(broker, e)
    e._open_close_intent("owner_request", "runbook close")
    monkeypatch.setattr(broker, "close_position",
                        lambda t: (_ for _ in ()).throw(RuntimeError("rejected")))

    outstanding = e._drive_close_intent(held, [])
    assert outstanding is True
    assert e._close_intent is not None, "an unconfirmed close was retired"
    assert e._close_intent.state != STATE_DONE
    assert e._entry_gate(broker.get_account_info())[0] is False, (
        "entries must stay shut while a close is outstanding"
    )


def test_an_unconfirmable_close_is_not_retired_either(broker, engine_factory, monkeypatch):
    """The broker cannot be read, so nothing is proven gone."""
    e = engine_factory(**live())
    held = filled_basket(broker, e)
    e._open_close_intent("owner_request", "runbook close")
    monkeypatch.setattr(broker, "get_open_positions",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("link down")))

    assert e._drive_close_intent(held, []) is True
    assert e._close_intent is not None
    assert e._close_intent.state != STATE_DONE


def test_an_order_left_resting_by_a_cancellation_race_keeps_the_close_open(broker, engine_factory):
    """The cancel is acknowledged, the order is still there on the re-read.

    Confirmed flat means no positions AND no resting orders. An order that
    survived cancellation is exposure waiting to happen, so the intent stays.
    """
    e = engine_factory(**live())
    held = filled_basket(broker, e)
    survivor = broker.place_pending_order("XAUUSD", PendingType.BUY_STOP, 0.01,
                                          broker.price + 5.0, "GRID", MAGIC)
    real_cancel = broker.cancel_pending_order
    broker.cancel_pending_order = lambda ticket: (
        None if ticket == survivor.ticket else real_cancel(ticket))

    e._open_close_intent("owner_request", "runbook close")
    outstanding = e._drive_close_intent(held, orders(broker))

    assert outstanding is True, "a surviving order must keep the close open"
    assert e._close_intent is not None and e._close_intent.state != STATE_DONE
    assert orders(broker), "fixture: the order was meant to survive"


# --- a confirmed close is retired, once --------------------------------------

def test_a_confirmed_close_is_retired_and_counted_once(broker, engine_factory):
    e = engine_factory(**live())
    held = filled_basket(broker, e)
    e._open_close_intent("basket_stop", "basket stop hit (-41.00)")

    assert e._drive_close_intent(held, []) is False
    assert e._close_intent is None, "a confirmed close kept its intent"
    assert e._baskets_stopped == 1

    # Driving again, and a further protective cycle, must not count it twice.
    e._protective_tick()
    e._protective_tick()
    assert e._baskets_stopped == 1, "the basket was counted more than once"


def test_a_loss_close_stays_latched_after_retirement(broker, engine_factory):
    e = engine_factory(**live())
    held = filled_basket(broker, e)
    e._open_close_intent("basket_stop", "basket stop hit (-41.00)")
    e._drive_close_intent(held, [])

    assert e._entries_paused is True, "a loss exit must leave entries paused"
    assert "basket_stop" in (e._pause_reason or "")
    allowed, reason = e._entry_gate(broker.get_account_info())
    assert allowed is False and reason.startswith("PAUSED"), reason

    broker.next_candle()
    e._tick()
    assert orders(broker) == [], "entries resurrected themselves after a loss exit"
    assert e._entries_paused is True


def test_retirement_does_not_erase_the_settlement_record(broker, engine_factory):
    """The intent goes; the evidence of what closed and why stays."""
    from app import db as db_module
    from app.db import TradeRecord

    e = engine_factory(**live())
    held = filled_basket(broker, e)
    tickets = {p.ticket for p in held}
    e._open_close_intent("basket_stop", "basket stop hit (-41.00)")
    e._drive_close_intent(held, [])
    e._reporting_tick()                       # settle and sync

    assert e._close_intent is None
    with db_module.SessionLocal() as session:
        rows = session.query(TradeRecord).filter(
            TradeRecord.account_id == e._account_id,
            TradeRecord.ticket.in_(tickets)).all()
        assert rows, "the closed tickets vanished from the trade table"
        assert all(r.status == "CLOSED" for r in rows)
        assert all(r.close_reason for r in rows), "the close reason was lost"
        assert all(r.profit is not None for r in rows), "the realised figure was lost"


def test_a_completed_close_does_not_survive_a_restart_as_an_open_request(broker, engine_factory):
    e = engine_factory(**live())
    held = filled_basket(broker, e)
    e._open_close_intent("basket_stop", "basket stop hit (-41.00)")
    e._drive_close_intent(held, [])
    assert e._close_intent is None

    fresh = engine_factory(**live())          # the backend restarts
    fresh._protective_tick()

    assert fresh._close_intent is None, "a finished close came back as an open request"
    assert fresh._entries_paused is True, "the latch did not survive the restart"
    assert orders(broker) == [], "a restarted engine placed a grid after a loss exit"


# --- what retirement is FOR --------------------------------------------------

def test_exposure_appearing_after_a_confirmed_close_is_managed_not_abandoned(broker, engine_factory):
    """MANAGED, which is not the same as closed on sight.

    After a basket stop the engine is not halted — it is paused. A position that
    appears afterwards is exposure it owns, so it must be marked, valued inside
    the basket, and closed when a limit fires. Force-closing anything that turns
    up would be a different bug. The halted case, where the exposure must go
    immediately, is `test_a_halted_engine_still_closes_exposure_that_appears_afterwards`.
    """
    e = engine_factory(**live())
    held = filled_basket(broker, e)
    e._open_close_intent("basket_stop", "basket stop hit (-41.00)")
    e._drive_close_intent(held, [])
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == []
    assert e._halt_reason is None, "a basket stop pauses; it does not halt"

    late = broker.open_position(OrderSide.BUY, broker.price, magic=MAGIC)
    e._protective_tick()

    assert late.ticket in e._last_marks, "the position was not even being marked"
    assert broker.get_open_positions("XAUUSD", magic=MAGIC), (
        "a position at no loss was closed on sight"
    )

    broker.price -= 70.0                      # now it is past the basket stop
    e._protective_tick()
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == [], (
        "owned exposure went past the basket stop and was not closed"
    )


def test_a_manual_trade_is_never_closed_by_any_of_this(broker, engine_factory):
    """Ownership is by magic number and retirement does not widen it."""
    e = engine_factory(**live())
    manual = broker.open_position(OrderSide.SELL, broker.price, magic=111222)
    held = filled_basket(broker, e)
    e._open_close_intent("basket_stop", "basket stop hit (-41.00)")
    e._drive_close_intent(held, [])
    e._protective_tick()
    e._reporting_tick()

    surviving = {p.ticket for p in broker.get_open_positions("XAUUSD")}
    assert manual.ticket in surviving, "the bot closed a trade it does not own"
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == []


# --- the three closure causes are not the same policy -------------------------
#
# The late-exposure test was rewritten once because it conflated them. Written
# out, so nothing conflates them again:
#
#   PROFIT close      -> non-latching. Entries may resume, same candle. Late
#                        owned exposure is MANAGED, and closed when a limit
#                        fires; it is not closed on sight.
#   BASKET STOP close -> latching. Entries stay paused until the owner resumes.
#                        Late owned exposure is still MANAGED under the limits.
#   LOSS HALT         -> the strongest state. Late owned exposure is CLOSED by
#                        the protective policy, immediately, and entries stay
#                        paused while that happens.

def profitable_basket(broker, engine):
    """A basket whose net clears its target, so the profit path closes it."""
    engine._tick()
    broker.next_candle()
    engine._tick()
    broker.price += 4.0                       # buy side fills
    broker.next_candle()
    engine._tick()
    for order in list(orders(broker)):
        broker.cancel_pending_order(order.ticket)
    broker.price += 6.0                       # and runs in favour
    engine._protective_tick()


def test_a_profit_close_retires_its_intent_without_latching(broker, engine_factory):
    e = engine_factory(**live(basket_take_profit_usd=10.0))
    profitable_basket(broker, e)

    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == []
    assert e._close_intent is None, "a confirmed profit close kept its intent"
    assert e._baskets_won == 1
    assert e._entries_paused is False, "a profit close must not latch entries"
    assert e._halt_reason is None
    # Not latched means admission is willing again. The `_profit_restart_pending`
    # flag is not asserted directly because the reporting half of the same tick
    # consumes it by placing the replacement — which is the behaviour itself.
    allowed, reason = e._entry_gate(broker.get_account_info())
    assert allowed is True, f"a profit close left entries refused: {reason}"


def test_a_basket_stop_close_retires_its_intent_and_does_latch(broker, engine_factory):
    e = engine_factory(**live())
    held = filled_basket(broker, e)
    e._open_close_intent("basket_stop", "basket stop hit (-41.00)")
    e._drive_close_intent(held, [])

    assert e._close_intent is None
    assert e._baskets_stopped == 1 and e._baskets_won == 0
    assert e._entries_paused is True, "a loss close must latch entries"
    assert e._halt_reason is None, "a basket stop is not a halt"
    assert e._profit_restart_pending is False, "a loss must not enable a replacement"


def test_late_exposure_after_a_profit_close_is_managed_not_closed_on_sight(broker, engine_factory):
    e = engine_factory(**live(basket_take_profit_usd=10.0))
    profitable_basket(broker, e)
    assert e._entries_paused is False

    late = broker.open_position(OrderSide.BUY, broker.price, magic=MAGIC)
    e._protective_tick()
    assert late.ticket in e._last_marks, "the position was not being marked"
    assert broker.get_open_positions("XAUUSD", magic=MAGIC), (
        "a position at no loss was closed on sight after a PROFIT close"
    )

    e._reporting_tick()
    assert orders(broker) == [], (
        "a new grid was placed on top of exposure that is already open"
    )

    broker.price -= 70.0                      # now past the basket stop
    e._protective_tick()
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == [], (
        "owned exposure passed the basket stop and was not closed"
    )


def test_late_exposure_after_a_loss_halt_is_closed_while_entries_stay_paused(broker,
                                                                            engine_factory):
    """The protective closure policy, and the pause, at the same time.

    A halt is the strongest refusal in the system. Exposure that appears after
    its close confirmed must be removed by the protective policy — and removing
    it must not be read as permission to trade again.
    """
    e = engine_factory(**live(max_daily_loss_usd=40.0))
    held = filled_basket(broker, e)
    broker.price -= 12.0                      # drive the day past its limit
    broker.next_candle()
    e._tick()
    assert e._halt_reason is not None and "daily loss" in e._halt_reason
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == []
    paused_reason = e._pause_reason

    late = broker.open_position(OrderSide.BUY, broker.price, magic=MAGIC)
    assert broker.get_open_positions("XAUUSD", magic=MAGIC), "fixture: exposure must exist"

    e._protective_tick()

    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == [], (
        "a halted engine left owned exposure open"
    )
    assert e._halt_reason is not None, "the halt was cleared by closing the exposure"
    assert e._entries_paused is True, "entries resumed while halted"
    assert e._pause_reason == paused_reason or e._pause_reason, "the pause reason vanished"
    allowed, reason = e._entry_gate(broker.get_account_info())
    assert allowed is False and reason.startswith("HALTED"), reason

    e._reporting_tick()
    assert orders(broker) == [], "a grid was placed while halted"
    assert late.ticket not in {p.ticket for p in broker.get_open_positions("XAUUSD")}


def test_a_halt_closes_late_exposure_repeatedly_not_only_once(broker, engine_factory):
    """Two late fills, two closures. The re-open branch must not be one-shot."""
    e = engine_factory(**live(max_daily_loss_usd=40.0))
    held = filled_basket(broker, e)
    broker.price -= 12.0
    broker.next_candle()
    e._tick()
    assert e._halt_reason is not None

    for _ in range(2):
        broker.open_position(OrderSide.BUY, broker.price, magic=MAGIC)
        e._protective_tick()
        assert broker.get_open_positions("XAUUSD", magic=MAGIC) == []
    assert e._entries_paused is True


def test_a_halt_does_not_close_a_manual_trade_it_does_not_own(broker, engine_factory):
    e = engine_factory(**live(max_daily_loss_usd=40.0))
    manual = broker.open_position(OrderSide.SELL, broker.price, magic=222333)
    filled_basket(broker, e)
    broker.price -= 12.0
    broker.next_candle()
    e._tick()
    assert e._halt_reason is not None

    broker.open_position(OrderSide.BUY, broker.price, magic=MAGIC)
    e._protective_tick()

    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == []
    assert manual.ticket in {p.ticket for p in broker.get_open_positions("XAUUSD")}, (
        "the halt reached a trade the bot does not own"
    )
