"""Symbol valuation: an unknown must stay unknown, all the way to admission.

The defect these pin down: `MT5Broker.get_symbol_info` read
`info.trade_tick_value or 1.0`, so a broker that reported no tick value produced
"one unit per point". Every figure downstream — the completed-grid estimate, the
basket target, the exit reserve — inherited that invention in silence, and
admission then let a grid through priced on it.

The adapter is exercised directly against a FAKE terminal object. No MetaTrader5
package is imported and no terminal is contacted: `MT5Broker` is constructed
without `__init__` and handed the double.
"""

import math
import threading

import pytest

from app.brokers.base import SymbolInfo
from app.brokers.mt5_broker import MT5Broker
from tests.conftest import MAGIC


class FakeSymbol:
    """MT5's symbol_info, with every field a test might want to break."""

    def __init__(self, **over):
        self.point = 0.01
        self.digits = 2
        self.trade_tick_value = 1.0
        self.trade_tick_size = 0.01
        self.volume_min = 0.01
        self.volume_step = 0.01
        self.trade_stops_level = 0
        for key, value in over.items():
            setattr(self, key, value)


class FakeQuote:
    def __init__(self, bid=4000.0, ask=4000.24):
        self.bid, self.ask = bid, ask


def adapter(symbol=None, quote=FakeQuote()):
    """An MT5Broker wired to doubles. Nothing here touches a terminal."""
    broker = MT5Broker.__new__(MT5Broker)
    broker._mt5 = type("FakeMT5", (), {
        "symbol_info": staticmethod(lambda s: symbol if symbol is not None else FakeSymbol()),
        "symbol_info_tick": staticmethod(lambda s: quote),
        "symbol_select": staticmethod(lambda s, e: True),
        "last_error": staticmethod(lambda: (0, "ok")),
    })()
    broker._lock = threading.Lock()
    broker._selected = {"XAUUSDm"}
    broker._ensure_symbol_selected = lambda s: None
    return broker


# --- the adapter refuses to invent ------------------------------------------

def test_a_healthy_specification_is_usable():
    info = adapter().get_symbol_info("XAUUSDm")
    assert info.valuation_ok is True
    assert info.valuation_problems == ()
    assert info.pip_value_per_lot == 1.0
    assert info.spread == pytest.approx(0.24)
    assert info.spread_available is True


@pytest.mark.parametrize("value", [0.0, None, float("nan"), float("inf"), -1.0, "abc"])
def test_an_unusable_tick_value_is_never_replaced_with_one(value):
    """The exact defect: `trade_tick_value or 1.0`."""
    info = adapter(FakeSymbol(trade_tick_value=value)).get_symbol_info("XAUUSDm")
    assert info.valuation_ok is False, f"{value!r} was accepted"
    assert info.pip_value_per_lot != 1.0, "a missing tick value became one unit per point"
    assert any("trade_tick_value" in p for p in info.valuation_problems)


@pytest.mark.parametrize("value", [0.0, None, float("nan"), -0.01])
def test_an_unusable_point_size_is_reported(value):
    info = adapter(FakeSymbol(point=value)).get_symbol_info("XAUUSDm")
    assert info.valuation_ok is False
    assert any("point" in p for p in info.valuation_problems)


def test_a_missing_tick_size_falls_back_to_the_point_but_says_nothing_is_wrong():
    """Tick size and point are the same quantity on these symbols; the VALUE is not."""
    info = adapter(FakeSymbol(trade_tick_size=0.0)).get_symbol_info("XAUUSDm")
    assert info.valuation_ok is True
    assert info.pip_value_per_lot == 1.0


# --- a missing quote is not a zero spread -----------------------------------

def test_no_quote_at_all_is_recorded_as_no_quote():
    info = adapter(quote=None).get_symbol_info("XAUUSDm")
    assert info.quote_available is False
    assert info.spread_available is False
    assert info.spread == 0.0, "the stored number is zero, but it is flagged as unavailable"
    assert info.usable_for_new_exposure is False


def test_a_genuinely_observed_zero_spread_is_usable():
    """Some feeds really do print a zero spread. That is data, not absence."""
    info = adapter(quote=FakeQuote(bid=4000.0, ask=4000.0)).get_symbol_info("XAUUSDm")
    assert info.quote_available is True
    assert info.spread_available is True
    assert info.spread == 0.0
    assert info.usable_for_new_exposure is True


@pytest.mark.parametrize("bid,ask", [
    (0.0, 4000.24), (4000.0, 0.0), (float("nan"), 4000.24), (4000.0, float("inf")),
    (None, 4000.24), (4000.24, 4000.0),        # crossed
])
def test_malformed_quotes_are_refused(bid, ask):
    info = adapter(quote=FakeQuote(bid=bid, ask=ask)).get_symbol_info("XAUUSDm")
    assert info.spread_available is False, f"bid={bid!r} ask={ask!r} was accepted"
    assert info.usable_for_new_exposure is False


# --- and admission refuses on all of it -------------------------------------

def unpriceable(**over):
    base = dict(symbol="XAUUSD", pip_size=0.01, pip_value_per_lot=1.0, min_volume=0.01,
                volume_step=0.01, spread=0.24)
    base.update(over)
    return SymbolInfo(**base)


def test_admission_refuses_when_the_valuation_is_unknown(broker, engine_factory, monkeypatch):
    e = engine_factory(basket_stop_loss_usd=60.0, max_daily_loss_usd=100.0)
    e._protective_tick()
    monkeypatch.setattr(broker, "get_symbol_info", lambda s: unpriceable(
        valuation_ok=False, valuation_problems=("trade_tick_value was not reported",)))

    allowed, reason = e._entry_gate(broker.get_account_info())
    assert allowed is False
    assert "cannot price this grid" in reason
    assert "trade_tick_value" in reason
    assert "still managed" in reason, "the refusal must not imply exposure was abandoned"


def test_admission_refuses_when_there_is_no_quote(broker, engine_factory, monkeypatch):
    e = engine_factory(basket_stop_loss_usd=60.0, max_daily_loss_usd=100.0)
    e._protective_tick()
    monkeypatch.setattr(broker, "get_symbol_info",
                        lambda s: unpriceable(spread_available=False, quote_available=False))

    allowed, reason = e._entry_gate(broker.get_account_info())
    assert allowed is False
    assert "not a zero spread" in reason


def test_admission_refuses_a_zero_estimate_from_an_unpriceable_symbol(broker, engine_factory,
                                                                     monkeypatch):
    """A 0.00 estimate must not read as a free grid."""
    e = engine_factory(basket_stop_loss_usd=60.0, max_daily_loss_usd=100.0)
    e._protective_tick()
    # Passes the adapter-level checks, but the money maths still cannot run.
    monkeypatch.setattr(broker, "get_symbol_info", lambda s: unpriceable(pip_value_per_lot=0.0))

    allowed, reason = e._entry_gate(broker.get_account_info())
    assert allowed is False
    assert "cannot be estimated" in reason
    assert "not a cheap grid" in reason


def test_an_unusable_price_is_refused(broker, engine_factory, monkeypatch):
    e = engine_factory(basket_stop_loss_usd=60.0, max_daily_loss_usd=100.0)
    e._protective_tick()
    monkeypatch.setattr(broker, "get_current_price", lambda s: float("nan"))

    allowed, reason = e._entry_gate(broker.get_account_info())
    assert allowed is False
    assert "unusable" in reason


def test_existing_exposure_is_still_protected_when_admission_data_is_missing(broker,
                                                                            engine_factory,
                                                                            monkeypatch):
    """Losing the ability to price a NEW grid must not stop protecting an old one."""
    # A basket stop ABOVE the completed-grid estimate, or admission refuses the
    # grid and there is nothing open to protect.
    e = engine_factory(basket_take_profit_usd=10_000.0, basket_stop_loss_usd=60.0,
                       max_daily_loss_usd=10_000.0)
    e._tick()
    broker.next_candle()
    e._tick()
    broker.price += 4.0
    broker.next_candle()
    e._tick()
    assert broker.get_open_positions("XAUUSD", magic=MAGIC), "fixture: a basket must be open"

    real_info = broker.get_symbol_info("XAUUSD")

    def broken_valuation(symbol):
        info = SymbolInfo(symbol=real_info.symbol, pip_size=real_info.pip_size,
                          pip_value_per_lot=real_info.pip_value_per_lot,
                          min_volume=real_info.min_volume, volume_step=real_info.volume_step,
                          spread=real_info.spread, profit_includes_exit_spread=False)
        info.valuation_ok = False
        info.valuation_problems = ("trade_tick_value was not reported",)
        return info

    monkeypatch.setattr(broker, "get_symbol_info", broken_valuation)
    broker.price -= 40.0                       # well past the 60.00 basket stop
    e._protective_tick()

    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == [], (
        "the basket stop stopped working because a NEW grid could not be priced"
    )
