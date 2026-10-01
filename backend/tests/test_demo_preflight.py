"""The demo pre-flight, verified offline against a FAKE terminal.

It is the tool that decides whether the owner connects a real demo account, so
the two properties that matter are tested here: it cannot trade, and it refuses a
REAL account before reading anything else.

No MetaTrader5 package is imported. A stub is installed in `sys.modules`, which
works because the tool imports MT5 inside `main()`.
"""

import sys
from datetime import datetime, timedelta, timezone

import pytest

from tools import demo_preflight

UTC = timezone.utc

DEMO, CONTEST, REAL = 0, 1, 2


class FakeAccount:
    def __init__(self, trade_mode=DEMO, balance=1000.0, equity=1000.0):
        self.trade_mode = trade_mode
        self.balance, self.equity = balance, equity
        self.currency, self.leverage = "USD", 500
        self.login, self.server = 12345678, "Exness-MT5Trial9"
        self.margin_mode = 2


class FakeSymbol:
    def __init__(self, **over):
        self.point, self.digits = 0.01, 2
        self.trade_tick_value, self.trade_tick_size = 1.0, 0.01
        self.volume_min, self.volume_step, self.trade_stops_level = 0.01, 0.01, 0
        for key, value in over.items():
            setattr(self, key, value)


class FakeQuote:
    def __init__(self, bid=4000.0, ask=4000.24):
        self.bid, self.ask, self.time_msc = bid, ask, 0


class FakeDeal:
    def __init__(self, symbol="XAUUSDm", volume=0.01, commission=-0.05, swap=-0.01, fee=0.0):
        self.symbol, self.volume = symbol, volume
        self.commission, self.swap, self.fee = commission, swap, fee


#: Distinguishes "the test did not specify a quote" from "there IS no quote".
#: Using None for both is how the first version of these tests silently handed a
#: healthy quote to the case that was meant to have none.
UNSET = object()


class FakeMT5:
    """Records every call so a test can assert what was and was not asked."""

    ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_CONTEST, ACCOUNT_TRADE_MODE_REAL = 0, 1, 2
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY = 0, 1, 2

    def __init__(self, account=None, symbol=None, quote=UNSET, deals=None,
                 selectable=True, positions=(), orders=()):
        self._account = account if account is not None else FakeAccount()
        self._symbol = symbol if symbol is not None else FakeSymbol()
        self._quote = FakeQuote() if quote is UNSET else quote
        self._deals = deals
        self._selectable = selectable
        self._positions, self._orders = list(positions), list(orders)
        self.calls: list[str] = []
        self.shutdown_called = False

    def initialize(self):
        self.calls.append("initialize")
        return True

    def account_info(self):
        self.calls.append("account_info")
        return self._account

    def symbol_select(self, symbol, enable):
        self.calls.append("symbol_select")
        return self._selectable

    def symbol_info(self, symbol):
        self.calls.append("symbol_info")
        return self._symbol

    def symbol_info_tick(self, symbol):
        self.calls.append("symbol_info_tick")
        return self._quote

    def history_deals_get(self, *a, **k):
        self.calls.append("history_deals_get")
        return self._deals

    def positions_get(self, symbol=None):
        self.calls.append("positions_get")
        return self._positions

    def orders_get(self, symbol=None):
        self.calls.append("orders_get")
        return self._orders

    def last_error(self):
        return (0, "ok")

    def shutdown(self):
        self.shutdown_called = True


@pytest.fixture
def terminal(monkeypatch):
    def install(**kwargs):
        stub = FakeMT5(**kwargs)
        monkeypatch.setitem(sys.modules, "MetaTrader5", stub)
        return stub
    return install


@pytest.fixture
def demo_settings(monkeypatch):
    """Settings as an owner mid-setup would have them."""
    from app.config import settings

    monkeypatch.setattr(settings, "symbol", "XAUUSDm", raising=False)
    monkeypatch.setattr(settings, "account_type", "demo", raising=False)
    monkeypatch.setattr(settings, "grid_buy_stop_levels", 10, raising=False)
    monkeypatch.setattr(settings, "grid_sell_stop_levels", 10, raising=False)
    monkeypatch.setattr(settings, "grid_lot_size", 0.01, raising=False)
    monkeypatch.setattr(settings, "grid_distance", 0.30, raising=False)
    monkeypatch.setattr(settings, "grid_magic_number", 990022, raising=False)
    monkeypatch.setattr(settings, "grid_capital_floor_usd", 0.0, raising=False)
    monkeypatch.setattr(settings, "grid_basket_stop_loss_usd", 0.0, raising=False)
    monkeypatch.setattr(settings, "grid_max_daily_loss_usd", 100.0, raising=False)
    monkeypatch.setattr(settings, "exit_commission_per_lot_usd", None, raising=False)
    monkeypatch.setattr(settings, "slippage_points_per_fill", None, raising=False)
    monkeypatch.setattr(settings, "broker_profit_includes_exit_spread", "unverified",
                        raising=False)
    return settings


# --- it cannot trade ---------------------------------------------------------

def test_the_source_contains_no_order_path():
    from pathlib import Path

    source = Path(demo_preflight.__file__).read_text()
    for forbidden in ("order_send", "order_check", "Buy(", "Sell(",
                      "place_pending_order", "close_position", "bot_manager",
                      "GridEngine", "from app.engine.grid_engine"):
        assert forbidden not in source, f"the pre-flight references {forbidden}"


def test_only_read_only_terminal_calls_are_made(terminal, demo_settings, capsys):
    stub = terminal(deals=[FakeDeal()])
    demo_preflight.main([])
    allowed = {"initialize", "account_info", "symbol_select", "symbol_info",
               "symbol_info_tick", "history_deals_get", "positions_get", "orders_get"}
    assert set(stub.calls) <= allowed, f"unexpected calls: {set(stub.calls) - allowed}"
    assert stub.shutdown_called


# --- a real account stops it dead -------------------------------------------

def test_a_real_account_is_refused_before_anything_else_is_read(terminal, demo_settings,
                                                               capsys):
    stub = terminal(account=FakeAccount(trade_mode=REAL))
    code = demo_preflight.main([])
    printed = capsys.readouterr().out

    assert code == 6
    assert "REAL account" in printed
    assert "Nothing further was read" in printed
    assert "symbol_info" not in stub.calls, "it kept reading after seeing a real account"
    assert stub.shutdown_called, "the terminal connection was left open"


def test_a_contest_account_is_flagged_but_not_fatal(terminal, demo_settings, capsys):
    terminal(account=FakeAccount(trade_mode=CONTEST))
    code = demo_preflight.main([])
    printed = capsys.readouterr().out
    assert "not 'demo'" in printed
    assert code == 1, "a non-demo classification must not report ready"


def test_a_disagreement_with_the_env_setting_is_fatal(terminal, demo_settings, capsys):
    demo_settings.account_type = "real"          # the app says real, the broker says demo
    terminal()
    code = demo_preflight.main([])
    printed = capsys.readouterr().out
    assert "disagreement" in printed or "disagrees" in printed
    assert code == 1


# --- it reports the owner's own numbers -------------------------------------

def test_it_prints_the_owners_completed_grid_estimate(terminal, demo_settings, capsys):
    """Fixture spread 0.24 with the app's x3 rule gives a 0.72 first step -> 46.20."""
    terminal()
    demo_preflight.main([])
    printed = capsys.readouterr().out
    assert "46.20" in printed, "the estimate for these inputs was not printed"
    assert "NOT a maximum loss" in printed
    assert "binding: THIS APP" in printed or "app_spread_multiple" in printed


def test_commission_is_measured_from_closed_deals_not_invented(terminal, demo_settings, capsys):
    """Two 0.10-lot deals charged 1.00 total -> 5.00 per lot both sides, 2.50 per side."""
    terminal(deals=[FakeDeal(volume=0.10, commission=-0.50, swap=0.0),
                    FakeDeal(volume=0.10, commission=-0.50, swap=0.0)])
    demo_preflight.main([])
    printed = capsys.readouterr().out
    assert "2.5 per lot per side" in printed or "2.5" in printed
    assert "EXIT_COMMISSION_PER_LOT_USD=2.5" in printed


def test_no_deal_history_says_so_instead_of_assuming_zero(terminal, demo_settings, capsys):
    terminal(deals=[])
    demo_preflight.main([])
    printed = capsys.readouterr().out
    assert "no closed deals" in printed
    assert "Nothing is invented here" in printed
    assert "EXIT_COMMISSION_PER_LOT_USD=0" not in printed, "a zero fee was assumed"


def test_deals_on_another_symbol_are_not_counted(terminal, demo_settings, capsys):
    terminal(deals=[FakeDeal(symbol="EURUSD", volume=1.0, commission=-7.0)])
    demo_preflight.main([])
    printed = capsys.readouterr().out
    assert "no closed XAUUSDm deals" in printed


# --- the unset settings become an actionable list ---------------------------

def test_unset_settings_are_listed_with_env_lines(terminal, demo_settings, capsys):
    terminal()
    code = demo_preflight.main([])
    printed = capsys.readouterr().out

    assert "GRID_CAPITAL_FLOOR_USD=" in printed
    assert "GRID_BASKET_STOP_LOSS_USD=" in printed
    assert "BROKER_PROFIT_INCLUDES_EXIT_SPREAD=no" in printed
    assert "yours to decide" in printed
    assert code == 0, "unset settings are not a fault, just work to do"


def test_a_risk_number_below_the_estimate_is_called_out(terminal, demo_settings, capsys):
    demo_settings.grid_basket_stop_loss_usd = 20.0      # below the 46.20 estimate
    demo_settings.grid_capital_floor_usd = 500.0
    terminal()
    code = demo_preflight.main([])
    printed = capsys.readouterr().out
    assert "admission will refuse every grid" in printed
    assert code == 1


def test_a_floor_with_too_little_headroom_is_called_out(terminal, demo_settings, capsys):
    demo_settings.grid_basket_stop_loss_usd = 60.0
    demo_settings.grid_capital_floor_usd = 980.0        # 20.00 of headroom vs 46.20
    terminal()
    code = demo_preflight.main([])
    printed = capsys.readouterr().out
    assert "headroom" in printed
    assert code == 1


def test_a_fully_configured_account_reports_ready(terminal, demo_settings, capsys):
    demo_settings.grid_basket_stop_loss_usd = 60.0
    demo_settings.grid_max_daily_loss_usd = 100.0
    demo_settings.grid_capital_floor_usd = 800.0
    demo_settings.exit_commission_per_lot_usd = 2.5
    demo_settings.slippage_points_per_fill = 1.0
    demo_settings.broker_profit_includes_exit_spread = "no"
    terminal(deals=[FakeDeal()])
    code = demo_preflight.main([])
    printed = capsys.readouterr().out

    assert code == 0
    assert "Every check this tool can make has passed" in printed
    assert "whether this strategy makes money" in printed, (
        "a ready verdict must still say what it does not establish"
    )


# --- broken specifications are refused, not defaulted -----------------------

def test_an_unusable_tick_value_stops_it(terminal, demo_settings, capsys):
    terminal(symbol=FakeSymbol(trade_tick_value=0.0))
    code = demo_preflight.main([])
    printed = capsys.readouterr().out
    assert "cannot price a grid" in printed
    assert code == 1


def test_a_missing_quote_stops_it(terminal, demo_settings, capsys):
    terminal(quote=None)
    code = demo_preflight.main([])
    printed = capsys.readouterr().out
    assert "no usable quote" in printed
    assert code == 1


def test_an_unselectable_symbol_stops_it(terminal, demo_settings, capsys):
    terminal(selectable=False)
    code = demo_preflight.main([])
    printed = capsys.readouterr().out
    assert "could not be selected" in printed
    assert "suffix" in printed
    assert code == 7


def test_exposure_owned_by_others_is_reported_as_untouchable(terminal, demo_settings, capsys):
    class P:
        def __init__(self, magic):
            self.magic = magic

    terminal(positions=[P(990022), P(111111)], orders=[P(222222)])
    demo_preflight.main([])
    printed = capsys.readouterr().out
    assert "1 position(s) and 0 order(s) carry this bot's magic" in printed
    assert "never be touched" in printed
