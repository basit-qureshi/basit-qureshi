"""The closing-cost contract: one answer, with provenance, and unknowns that block.

The defect this addresses had three parts. `_estimated_exit_cost` returned an
UNVERIFIED half-spread; the daily loss trigger deliberately ignored it, so a
known cost could not bring a protective exit forward; and admission ignored the
exit entirely while comparing an entry-side estimate against a budget. The fit
report then printed a fourth arithmetic of its own.

Reviewer's structural case, pinned below: marked loss 99.00 with a 2.00 closing
cost against a 100.00 daily limit. The money left after closing is 101.00 of
loss, past the limit, and the old reading of 99.00 did not fire.
"""

import pytest

from app.brokers.base import SymbolInfo
from app.engine import costs
from tests.conftest import MAGIC

GOLD = dict(pip_size=0.01, pip_value_per_lot=1.0, spread=0.24)


def inputs(**over):
    base = dict(**GOLD)
    base.update(over)
    return costs.ClosingCostInputs(**base)


# --- the four quantities stay apart -----------------------------------------

def test_a_verified_included_exit_spread_is_not_charged_twice():
    """If the broker's figure already contains the exit, adding it doubles it."""
    cost = costs.closing_cost(0.20, 20, inputs(profit_includes_exit_spread=True,
                                               exit_commission_per_lot=0.0,
                                               slippage_points_per_fill=0.0))
    spread = next(c for c in cost.components if c.name == "exit_spread")
    assert spread.value == 0.0 and spread.provenance == costs.VERIFIED
    assert "already" in spread.note
    assert cost.defensible == 0.0


def test_a_verified_excluded_exit_spread_is_charged_once():
    """0.24 spread / 0.01 point = 24 points; half is 12; 12 * 0.20 lots * 1.00 = 2.40."""
    cost = costs.closing_cost(0.20, 20, inputs(profit_includes_exit_spread=False,
                                               exit_commission_per_lot=0.0,
                                               slippage_points_per_fill=0.0))
    assert cost.defensible == pytest.approx(2.40)
    assert cost.conservative == pytest.approx(2.40)


def test_an_unverified_semantic_bounds_a_profit_exit_but_cannot_fire_a_loss_exit():
    """The asymmetry the whole contract exists for."""
    cost = costs.closing_cost(0.20, 20, inputs(profit_includes_exit_spread=None,
                                               exit_commission_per_lot=0.0,
                                               slippage_points_per_fill=0.0))
    assert cost.conservative == pytest.approx(2.40), "no upper bound for delaying a close"
    assert cost.defensible is None, "an unverified assumption fired a loss exit"
    assert "exit_spread_semantic" in cost.unknowns


def test_booked_charges_are_never_added_again():
    cost = costs.closing_cost(0.20, 20, inputs(profit_includes_exit_spread=False,
                                               exit_commission_per_lot=0.0,
                                               slippage_points_per_fill=0.0))
    assert any("ALREADY booked" in note for note in cost.notes), (
        "nothing states that booked swap and commission are excluded"
    )


def test_a_missing_commission_is_unknown_not_zero():
    cost = costs.closing_cost(0.20, 20, inputs(profit_includes_exit_spread=False,
                                               slippage_points_per_fill=0.0))
    assert cost.defensible is None
    assert "exit_commission" in cost.unknowns
    assert "EXIT_COMMISSION_PER_LOT_USD" in cost.missing_inputs_message()


def test_a_missing_slippage_is_unknown_not_zero():
    cost = costs.closing_cost(0.20, 20, inputs(profit_includes_exit_spread=False,
                                               exit_commission_per_lot=0.0))
    assert cost.defensible is None
    assert "slippage" in cost.unknowns
    assert "SLIPPAGE_POINTS_PER_FILL" in cost.missing_inputs_message()


def test_owner_supplied_costs_are_used_and_labelled():
    cost = costs.closing_cost(0.20, 20, inputs(profit_includes_exit_spread=False,
                                               exit_commission_per_lot=2.75,
                                               slippage_points_per_fill=1.0))
    # spread 2.40 + commission 2.75*0.20 = 0.55 + slippage 1*0.20*1.00 = 0.20
    assert cost.defensible == pytest.approx(3.15)
    provenances = {c.name: c.provenance for c in cost.components}
    assert provenances["exit_commission"] == costs.OWNER_STATED
    assert provenances["slippage"] == costs.OWNER_STATED
    assert provenances["exit_spread"] == costs.VERIFIED


def test_no_quote_means_the_spread_term_is_unknown():
    cost = costs.closing_cost(0.20, 20, inputs(spread_available=False,
                                               exit_commission_per_lot=0.0,
                                               slippage_points_per_fill=0.0))
    assert cost.defensible is None
    assert "spread" in cost.unknowns


def test_an_unpriceable_symbol_states_nothing_at_all():
    cost = costs.closing_cost(0.20, 20, inputs(pip_value_per_lot=0.0))
    assert cost.conservative is None and cost.defensible is None
    assert "symbol_valuation" in cost.unknowns


# --- the engine reads one contract ------------------------------------------

def test_the_engine_exposes_both_readings(broker, engine_factory):
    from app.brokers.base import OrderSide

    e = engine_factory(basket_stop_loss_usd=60.0, max_daily_loss_usd=100.0)
    broker.open_position(OrderSide.BUY, broker.price, magic=MAGIC)
    positions = broker.get_open_positions("XAUUSD", magic=MAGIC)

    conservative = e._estimated_exit_cost(positions)
    defensible = e._defensible_exit_cost(positions)
    assert conservative is not None and defensible is not None
    assert conservative == defensible, (
        "with a verified semantic and stated costs the two readings must agree"
    )


def test_an_unverified_semantic_leaves_the_day_reading_on_marked_money(broker, engine_factory,
                                                                      monkeypatch):
    """No defensible cost means no reserve in the trigger — and no early firing."""
    from app.brokers.base import OrderSide

    e = engine_factory(basket_stop_loss_usd=60.0, max_daily_loss_usd=100.0)
    e._protective_tick()
    real = broker.get_symbol_info("XAUUSD")
    monkeypatch.setattr(broker, "get_symbol_info", lambda s: SymbolInfo(
        s, real.pip_size, real.pip_value_per_lot, 0.01, 0.01, spread=real.spread,
        profit_includes_exit_spread=None))
    broker.open_position(OrderSide.BUY, broker.price, magic=MAGIC)

    risk = e._day_risk(broker.get_open_positions("XAUUSD", magic=MAGIC))
    assert risk.exit_reserve is None, "an unverified reserve reached the decision path"
    assert risk.risk_reading == risk.marked_result


def test_a_known_closing_cost_brings_the_daily_limit_forward(broker, engine_factory):
    """Reviewer's case, through the real trigger.

    Settled loss 99.00, a known closing cost, a 100.00 limit. The reading that
    decides is the money that would be left after closing.
    """
    from app.brokers.base import OrderSide
    from app import db as db_module
    from app.db import TradeRecord

    e = engine_factory(basket_stop_loss_usd=1000.0, max_daily_loss_usd=100.0,
                       basket_take_profit_usd=10_000.0,
                       exit_commission_per_lot=10.0, slippage_points_per_fill=0.0)
    # 10.00 per lot on 0.10 lots is 1.00 of commission, which is what takes the
    # reading from -99.xx to past -100.00.
    e._protective_tick()
    with db_module.SessionLocal() as session:
        session.add(TradeRecord(account_id=e._account_id, ticket="settled-99", symbol="XAUUSD",
                                side="BUY", volume=0.01, open_price=4000.0, sl=0, tp=0,
                                profit=-99.0, mode="demo", status="CLOSED", magic=MAGIC,
                                trading_day=e._trading_day))
        session.commit()
    e._refresh_daily_totals()

    broker.open_position(OrderSide.BUY, broker.price, magic=MAGIC, volume=0.10)
    positions = broker.get_open_positions("XAUUSD", magic=MAGIC)
    risk = e._day_risk(positions)
    assert risk.marked_result > -100.0, "fixture: marked money alone must NOT breach"
    assert risk.exit_reserve is not None and risk.exit_reserve > 0
    assert risk.risk_reading <= -100.0, "fixture: the reading after costs must breach"

    e._protective_tick()
    assert e._halt_reason is not None, (
        "a known closing cost that puts the day past its limit did not fire the trigger"
    )
    assert "closing cost" in e._halt_reason
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == []


def test_admission_blocks_when_closing_costs_are_unknown(broker, engine_factory):
    """Not invented, not assumed zero: refused, with the settings named."""
    e = engine_factory(basket_stop_loss_usd=60.0, max_daily_loss_usd=100.0,
                       exit_commission_per_lot=None, slippage_points_per_fill=None)
    e._protective_tick()

    allowed, reason = e._entry_gate(broker.get_account_info())
    assert allowed is False
    assert "closing costs" in reason
    assert "EXIT_COMMISSION_PER_LOT_USD" in reason and "SLIPPAGE_POINTS_PER_FILL" in reason
    assert "still managed" in reason
    assert "assumed to be zero" in reason


def test_unknown_closing_costs_do_not_stop_protecting_what_is_open(broker, engine_factory):
    """The gate governs new baskets only."""
    from app.brokers.base import OrderSide

    e = engine_factory(basket_stop_loss_usd=20.0, max_daily_loss_usd=10_000.0,
                       basket_take_profit_usd=10_000.0,
                       exit_commission_per_lot=None, slippage_points_per_fill=None)
    e._protective_tick()
    broker.open_position(OrderSide.BUY, broker.price, magic=MAGIC)
    broker.price -= 40.0                      # past the 20.00 basket stop

    e._protective_tick()
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == [], (
        "an unknown closing cost stopped the basket stop from working"
    )


def test_the_owner_can_state_the_broker_semantic(broker, engine_factory):
    """`yes` makes the exit spread verified-as-included, which is 0.00."""
    from app.brokers.base import OrderSide

    e = engine_factory(basket_stop_loss_usd=60.0, max_daily_loss_usd=100.0,
                       exit_commission_per_lot=0.0, slippage_points_per_fill=0.0,
                       broker_profit_includes_exit_spread="yes")
    broker.open_position(OrderSide.BUY, broker.price, magic=MAGIC)
    positions = broker.get_open_positions("XAUUSD", magic=MAGIC)
    assert e._defensible_exit_cost(positions) == 0.0
