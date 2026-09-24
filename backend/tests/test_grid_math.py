"""The completed-grid estimate, checked against hand arithmetic.

These tests exist because a number that drives an admission refusal has to be
derivable by hand from stated inputs. Every expected value below is computed in
its own docstring, not copied from the implementation's output.

They also pin the two claims the reports make about it:

  * it is NOT symbol- or currency-specific in the code — the same arithmetic
    produces a different number from a different specification, and $37.80 is
    what THIS PROJECT'S FIXTURE produces, nothing more
  * the gate measures the grid the engine actually places
"""

import pytest

from app.engine import grid_math
from tests.conftest import MAGIC

GOLD_FIXTURE = grid_math.SymbolSpec(pip_size=0.01, pip_value_per_lot=1.0,
                                    spread=0.24, min_stop_distance=0.0)
TEN_BY_TEN = grid_math.GridSpec(buy_levels=10, sell_levels=10, lot=0.01, distance=0.30)


def test_the_fixture_gold_estimate_is_derivable_by_hand():
    """displacement: 0.30 * (1+...+10) = 16.50 per side, 33.00 both sides.
    33.00 price units / 0.01 point = 3300 points; 3300 * 0.01 lot * 1.0 = 33.00.
    entry spread: 0.24 / 0.01 = 24 points; 24 * 0.01 * 1.0 * 20 fills = 4.80.
    total = 37.80.
    """
    est = grid_math.completed_grid_estimate(4000.0, TEN_BY_TEN, GOLD_FIXTURE)
    assert est.displacement_cost == 33.00
    assert est.entry_spread_cost == 4.80
    assert est.total == 37.80
    assert est.fills == 20 and est.total_volume == 0.20


def test_the_number_is_not_a_property_of_gold():
    """A forex-shaped specification: pip 0.0001, 10.0 per pip per lot, 0.0002 spread.

    displacement: 0.0010 first step and spacing -> 0.0010*(1+..+5) = 0.0150 per
    side at 5 levels, 0.0300 both sides. 0.0300 / 0.0001 = 300 pips;
    300 * 0.10 lot * 10.0 = 300.00.
    entry spread: 0.0002/0.0001 = 2 pips; 2 * 0.10 * 10.0 * 10 fills = 20.00.
    total = 320.00 — same arithmetic, nothing gold about it.
    """
    forex = grid_math.SymbolSpec(pip_size=0.0001, pip_value_per_lot=10.0, spread=0.0002)
    grid = grid_math.GridSpec(buy_levels=5, sell_levels=5, lot=0.10, distance=0.0010)
    est = grid_math.completed_grid_estimate(1.2000, grid, forex)
    assert est.displacement_cost == 300.00
    assert est.entry_spread_cost == 20.00
    assert est.total == 320.00


def test_the_estimate_scales_with_the_account_currency_value():
    """pip_value_per_lot carries the currency conversion, so doubling it doubles
    the estimate. Nothing in the code assumes USD."""
    doubled = grid_math.SymbolSpec(pip_size=0.01, pip_value_per_lot=2.0, spread=0.24)
    base = grid_math.completed_grid_estimate(4000.0, TEN_BY_TEN, GOLD_FIXTURE).total
    other = grid_math.completed_grid_estimate(4000.0, TEN_BY_TEN, doubled).total
    assert other == pytest.approx(base * 2, abs=0.01)


def test_a_wider_minimum_stop_distance_raises_it_more_than_linearly():
    """MT5's minimum stop distance is derived from the live spread (spread*3 in
    this adapter), so a wider spread pushes EVERY level further out.

    At min_stop_distance 0.75: displacement 10*0.75 + 0.30*(0+..+9) = 21.00 per
    side, 42.00 both. 42.00/0.01 * 0.01 * 1.0 = 42.00. Entry spread at 0.25:
    25 points * 0.01 * 1.0 * 20 = 5.00. Total 47.00 — against 37.80 at the
    fixture's 0.24 spread and zero minimum.
    """
    wider = grid_math.SymbolSpec(pip_size=0.01, pip_value_per_lot=1.0,
                                 spread=0.25, min_stop_distance=0.75)
    est = grid_math.completed_grid_estimate(4000.0, TEN_BY_TEN, wider)
    assert est.first_step == 0.75
    assert est.displacement_cost == 42.00
    assert est.entry_spread_cost == 5.00
    assert est.total == 47.00


def test_an_unreadable_symbol_specification_returns_zero_rather_than_a_guess():
    unknown = grid_math.SymbolSpec(pip_size=0.0, pip_value_per_lot=1.0, spread=0.24)
    est = grid_math.completed_grid_estimate(4000.0, TEN_BY_TEN, unknown)
    assert est.total == 0.0, "a missing specification must not be filled in"


def test_unequal_sides_are_priced_as_configured():
    """10 buys + 5 sells: displacement 16.50 + 0.30*(1+..+5)=4.50 -> 21.00.
    entry spread 24 points * 0.01 * 1.0 * 15 fills = 3.60. Total 24.60."""
    grid = grid_math.GridSpec(buy_levels=10, sell_levels=5, lot=0.01, distance=0.30)
    est = grid_math.completed_grid_estimate(4000.0, grid, GOLD_FIXTURE)
    assert est.total == 24.60
    assert est.fills == 15


def test_the_estimate_carries_its_own_exclusions():
    """A caller cannot print the number without what it leaves out."""
    est = grid_math.completed_grid_estimate(4000.0, TEN_BY_TEN, GOLD_FIXTURE)
    joined = " ".join(est.excluded).lower()
    for missing in ("exit", "swap", "partial", "slippage", "conversion"):
        assert missing in joined


# --- the engine, the gate and the report must all agree ----------------------

def test_the_engine_uses_the_same_arithmetic(broker, engine_factory):
    e = engine_factory(basket_stop_loss_usd=60.0)
    info = broker.get_symbol_info("XAUUSD")
    price = broker.get_current_price("XAUUSD")
    expected = grid_math.completed_grid_estimate(
        price,
        grid_math.GridSpec(buy_levels=e.buy_stop_levels, sell_levels=e.sell_stop_levels,
                           lot=e.lot_size, distance=e.grid_distance),
        grid_math.SymbolSpec.from_broker(info)).total
    assert e._completed_grid_loss(price, info) == expected


def test_the_gate_measures_the_grid_that_is_actually_placed(broker, engine_factory):
    """Two copies of the level arithmetic would let admission price a grid the
    engine does not place. The placed orders are compared with the estimate's."""
    e = engine_factory(basket_stop_loss_usd=60.0, max_daily_loss_usd=100.0)
    info = broker.get_symbol_info("XAUUSD")
    price = broker.get_current_price("XAUUSD")
    est = grid_math.completed_grid_estimate(
        price,
        grid_math.GridSpec(buy_levels=e.buy_stop_levels, sell_levels=e.sell_stop_levels,
                           lot=e.lot_size, distance=e.grid_distance),
        grid_math.SymbolSpec.from_broker(info))

    e._tick()
    broker.next_candle()
    e._tick()
    placed = sorted(o.price for o in broker.get_pending_orders("XAUUSD", magic=MAGIC))
    assert placed == sorted(est.sell_levels + est.buy_levels), (
        "the orders the engine placed are not the levels the gate measured"
    )


def test_the_offline_report_prints_the_same_number(capsys):
    from tools import grid_fit_report

    assert grid_fit_report.main(["--basket-stop", "60"]) == 0
    printed = capsys.readouterr().out
    expected = grid_math.completed_grid_estimate(4000.0, TEN_BY_TEN, GOLD_FIXTURE).total
    assert f"{expected:.2f}" in printed
    assert "FIXTURE" in printed, "the report must label fixture inputs as fixtures"
    assert "NOT a maximum loss" in printed


def test_the_report_judges_only_the_budgets_it_was_given(capsys):
    from tools import grid_fit_report

    grid_fit_report.main([])
    printed = capsys.readouterr().out
    assert "none stated" in printed
    assert "REFUSED" not in printed, "nothing may be judged against a budget nobody gave"
