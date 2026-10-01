"""The two engines, and the claims the toggle between them is allowed to make.

The claim under test is narrow and it is the whole point: **the grid is the
same**. These tests pin that by construction — the vendored file is diffed
against the commit it came from, the adapter is checked for strategy overrides
by name, and both engines are asked to build a grid from identical settings and
compared order by order.

The second claim is the difference, and it is demonstrated rather than
described: on the owner's own account size the guarded engine refuses and the
2 September engine places the grid, because the 2 September engine has no entry
gate at all.

What is deliberately NOT claimed anywhere here: that either engine is
profitable, or that one trades better than the other. Nothing in this repository
measures that.
"""

import subprocess
from pathlib import Path

import pytest

from app.engine import profiles as engine_profiles
from app.engine.grid_engine import GridEngine
from app.engine.original_adapter import OriginalEngine
from app.engine.original_engine import OriginalGridEngine
from tests.conftest import MAGIC, FakeBroker

REPO = Path(__file__).resolve().parents[2]
PINNED_COMMIT = "5aff68a"
ORIGINAL_FILE = REPO / "backend" / "app" / "engine" / "original_engine.py"


# --------------------------------------------------------------- provenance

def _git(*args) -> str | None:
    try:
        done = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def test_the_vendored_file_is_the_pinned_commit_with_two_known_edits():
    """The vendored engine must stay the 2 September bot, byte for byte.

    If this fails, someone edited it. That is not a conflict to resolve by
    accepting the new side: the file's only purpose is to be what the bot was on
    that date, and an edited copy is not that.
    """
    original = _git("show", f"{PINNED_COMMIT}:backend/app/engine/grid_engine.py")
    if original is None:
        pytest.skip("git history is not available here")
    vendored = ORIGINAL_FILE.read_text()

    # Edit 1: a provenance header was prepended, ending at the original's first line.
    marker = '"""XAUUSD M1 pending-order grid.'
    assert marker in vendored
    body = vendored[vendored.index(marker):]

    # Edit 2: the class was renamed so both engines can be imported at once.
    assert body.replace("class OriginalGridEngine:", "class GridEngine:") == original


def test_the_header_names_the_commit_and_the_date():
    head = ORIGINAL_FILE.read_text()[:2500]
    assert PINNED_COMMIT in head
    assert "2 September" in head
    assert "original_adapter" in head


def test_the_vendored_engine_really_has_no_entry_gate():
    """The absence IS the feature. Named here so it cannot be added back quietly."""
    for absent in ("_entry_gate", "_affordability", "_completed_grid_loss",
                   "capital_reserve_percent", "capital_floor_usd"):
        assert not hasattr(OriginalGridEngine, absent), absent
    # Scanned below the provenance header, which names both absences in prose.
    body = ORIGINAL_FILE.read_text().split('"""XAUUSD M1 pending-order grid.', 1)[1]
    assert "capital_reserve" not in body
    assert "capital_floor" not in body


# ------------------------------------------------- the adapter's boundaries

#: Methods that decide what is traded, when, at what size, and when it ends —
#: plus the bookkeeping they depend on. The adapter may not override any of
#: them; if it did, "the 2 September bot" would be a different bot wearing the
#: name.
STRATEGY_METHODS = (
    "_tick",
    "_loop",
    "start",
    "_build_grid",
    "_close_everything",
    "_check_risk_limits",
    "_check_daily_target",
    "_roll_day",
    "_gate_status",
    "_arm_gate",
    "_own_state_exists",
    "_within_session",
    "_is_hedged",
    "_current_candle_time",
    "_current_pendings",
    "_record_new_fills",
    "_settle_closed_trades",
    "_refresh_daily_totals",
    "_trading_day_for",
)


@pytest.mark.parametrize("name", STRATEGY_METHODS)
def test_the_adapter_overrides_no_strategy_method(name):
    assert name not in OriginalEngine.__dict__, (
        f"OriginalEngine overrides {name}, which is a strategy decision or the bookkeeping it "
        "depends on. The adapter may add reporting and owner controls only."
    )
    assert getattr(OriginalEngine, name) is getattr(OriginalGridEngine, name)


def test_the_adapter_overrides_are_all_declared_plumbing():
    """A new override has to be a deliberate decision, not a quiet one."""
    allowed = {
        "__init__", "__module__", "__qualname__", "__doc__", "__dict__", "__weakref__",
        "profile", "_account_id", "_NO_PAUSE",
        "stop", "pause_entries", "resume_entries", "close_and_pause", "clear_halt",
        "verify_account", "_basket_pnl_display", "_estimated_exit_cost", "_day_risk",
        "_observation_header", "_profile_report", "session_label", "status", "_broadcast",
    }
    assert set(OriginalEngine.__dict__) <= allowed, set(OriginalEngine.__dict__) - allowed


# ------------------------------------------------------ the same grid, twice

def _settings(**over):
    base = dict(
        lot_size=0.02, buy_stop_levels=7, sell_stop_levels=4, grid_distance=0.45,
        basket_take_profit_usd=12.0, basket_stop_loss_usd=40.0, magic_number=MAGIC,
        max_daily_loss_usd=500.0,
    )
    base.update(over)
    return base


GUARDED_EXTRA = dict(capital_floor_usd=10.0, capital_reserve_percent=50.0,
                     exit_commission_per_lot=0.0, slippage_points_per_fill=0.0)


def test_both_engines_place_the_identical_grid():
    """Same settings, same price, same orders — side, price and volume."""
    placed = {}
    for key, build, extra in (("guarded", GridEngine, GUARDED_EXTRA),
                              ("original", OriginalEngine, {})):
        broker = FakeBroker(price=4000.0, balance=5000.0)
        engine = build(broker=broker, symbol="XAUUSD", mode="demo", **_settings(**extra))
        engine._build_grid()
        placed[key] = sorted(
            (o.order_type.value, round(o.price, 5), o.volume)
            for o in broker.get_pending_orders("XAUUSD", magic=MAGIC)
        )

    assert placed["guarded"] == placed["original"]
    assert len(placed["guarded"]) == 11  # 7 buy + 4 sell, both sides unchanged


def test_both_engines_use_the_same_first_step_and_spacing():
    broker = FakeBroker(price=4000.0, balance=5000.0)
    broker.spread = 0.0  # so min_stop_distance cannot differ between the two reads
    engine = OriginalEngine(broker=broker, symbol="XAUUSD", mode="demo", **_settings())
    engine._build_grid()
    buys = sorted(o.price for o in broker.get_pending_orders("XAUUSD", magic=MAGIC)
                  if o.order_type.value == "BUY_STOP")
    assert buys[0] == pytest.approx(4000.0 + 0.45)
    assert buys[1] - buys[0] == pytest.approx(0.45)


def test_both_engines_wait_for_the_next_candle_before_the_first_grid():
    """The one gate that is strategy, and is present on both sides."""
    for build, extra in ((GridEngine, GUARDED_EXTRA), (OriginalEngine, {})):
        broker = FakeBroker(price=4000.0, balance=5000.0)
        engine = build(broker=broker, symbol="XAUUSD", mode="demo", **_settings(**extra))
        engine._tick()
        assert broker.get_pending_orders("XAUUSD", magic=MAGIC) == []
        broker.next_candle()
        engine._tick()
        assert len(broker.get_pending_orders("XAUUSD", magic=MAGIC)) == 11


# ------------------------------------------- what the switch actually changes

def test_the_original_trades_the_account_the_guarded_engine_refuses():
    """The owner's own case, as the toggle's reason for existing.

    A $195 balance, a 10+10 grid at 0.01 lots, a $170 capital floor. The guarded
    engine refuses: the completed-grid estimate exceeds the headroom above the
    floor. The 2 September engine has no such rule and places the grid. Both
    place the SAME grid when they place one — this is a difference in admission,
    not in strategy.
    """
    settings = dict(lot_size=0.01, buy_stop_levels=10, sell_stop_levels=10,
                    grid_distance=0.30, basket_take_profit_usd=2.50,
                    basket_stop_loss_usd=0.0, max_daily_loss_usd=500.0,
                    magic_number=MAGIC)

    guarded_broker = FakeBroker(price=4000.0, balance=195.24)
    guarded = GridEngine(broker=guarded_broker, symbol="XAUUSD", mode="demo",
                         capital_floor_usd=170.0, capital_reserve_percent=50.0,
                         exit_commission_per_lot=0.0, slippage_points_per_fill=0.0,
                         **settings)
    account = guarded_broker.get_account_info()
    guarded._ensure_day_bound(account)   # so the refusal is the floor, not the anchor
    allowed, reason = guarded._entry_gate(account)
    assert allowed is False
    assert "headroom" in reason, reason

    broker = FakeBroker(price=4000.0, balance=195.24)
    engine = OriginalEngine(broker=broker, symbol="XAUUSD", mode="demo", **settings)
    engine._tick()
    broker.next_candle()
    engine._tick()
    assert len(broker.get_pending_orders("XAUUSD", magic=MAGIC)) == 20


def test_the_original_judges_the_basket_on_gross_profit(original_engine_factory, broker):
    """Swap and commission are not inside the number that triggers a close."""
    engine = original_engine_factory(basket_take_profit_usd=2.0)
    position = broker.open_position("BUY", 4000.0, magic=MAGIC)
    position.swap, position.commission = -1.5, -1.0
    broker.price = 4002.50          # +$2.50 gross, +$0.00 net after booked costs

    net, gross, costs_known = engine._basket_pnl_display(
        broker.get_open_positions("XAUUSD", magic=MAGIC))
    assert gross == pytest.approx(2.5)
    assert net == gross, "this engine reports what it acts on, and it acts on gross"
    assert costs_known is False
    assert engine._estimated_exit_cost([]) is None


def test_the_original_halt_is_not_durable(original_engine_factory):
    """`start()` clears it, and nothing is written down. Stated, not hidden."""
    engine = original_engine_factory()
    engine._halt_reason = "daily loss limit reached"
    ok, message = engine.clear_halt()
    assert ok is True
    assert "no durable halt" in message
    assert engine._halt_reason is None


def test_clearing_a_halt_that_was_never_set_says_so(original_engine_factory):
    ok, message = original_engine_factory().clear_halt()
    assert ok is True and message == "No halt was set."


def test_the_original_records_trades_in_the_bucket_it_writes(original_engine_factory, broker):
    """Pinned to `legacy`, which is where the vendored engine's rows land and
    where the existing trade history already is."""
    engine = original_engine_factory()
    assert engine._account_id == "legacy"

    engine._tick()
    broker.next_candle()
    engine._tick()                  # the grid is built here, at 4000
    broker.price = 4010.0           # runs through every buy stop
    engine._tick()                  # which is where the fills are recorded

    from app import db as db_module
    from app.db import TradeRecord
    with db_module.SessionLocal() as session:
        rows = session.query(TradeRecord).filter_by(magic=MAGIC).all()
    assert rows, "no fills were recorded"
    assert {r.account_id for r in rows} == {"legacy"}


def test_the_original_reports_what_it_is_not_running(original_engine_factory):
    report = original_engine_factory(capital_floor_usd=170.0,
                                     capital_reserve_percent=50.0).status()
    assert report["engine_profile"] == "original"
    assert report["engine_profile_source_commit"] == "5aff68a"
    assert "capital_floor_usd" in report["settings_not_applied"]
    assert "capital_reserve_percent" in report["settings_not_applied"]
    assert report["capital_floor_usd"] is None
    assert report["capital_reserve_percent"] is None
    assert any("no entry gate at all" in line for line in report["engine_profile_missing"])
    assert any("GROSS" in line for line in report["engine_profile_missing"])
    assert any("not durable" in line for line in report["engine_profile_missing"])


def test_the_guarded_engine_names_itself_too(engine_factory):
    assert engine_factory().status()["engine_profile"] == "guarded"


# --------------------------------------------------------- the status surface

DASHBOARD_KEYS = (
    "running", "mode", "symbol", "connected", "engine_profile", "entries_paused",
    "pause_reason", "halted", "entry_blocked", "entry_block_reason", "day_risk",
    "liquidation_policy", "persistence_error", "trading_window", "in_session",
    "today_net_profit_usd", "snapshot_seq", "observation_account_id", "account_id",
)


@pytest.mark.parametrize("key", DASHBOARD_KEYS)
def test_both_engines_answer_every_key_the_dashboard_reads(key, engine_factory, original_engine_factory):
    assert key in engine_factory().status()
    assert key in original_engine_factory().status()


def test_the_original_day_risk_says_unavailable_rather_than_zero(original_engine_factory):
    """Zero would be a confident number for a measure this engine never computes."""
    reading = original_engine_factory().status()["day_risk"]
    assert reading["available"] is False
    assert reading["marked_result"] is None
    assert "realised" in reading["reason"]


def test_the_original_snapshot_sequence_increases(original_engine_factory):
    engine = original_engine_factory()
    first = engine.status()["snapshot_seq"]
    assert engine.status()["snapshot_seq"] > first


def test_the_original_broadcast_carries_the_snapshot_header(original_engine_factory):
    seen = []
    engine = original_engine_factory(on_update=seen.append)
    engine._tick()
    assert seen, "the engine broadcast nothing"
    payload = seen[-1]
    assert payload["type"] == "tick"
    assert payload["engine_profile"] == "original"
    assert isinstance(payload["snapshot_seq"], int)
    assert payload["positions_known"] is True
    assert "grid" in payload


# ------------------------------------------------------------ owner controls

@pytest.mark.parametrize("control", ("pause_entries", "resume_entries"))
def test_pause_controls_refuse_instead_of_pretending(control, original_engine_factory):
    """There is no pause here, and a button that always refuses is better than
    one that appears to hold entries and does not."""
    ok, message = getattr(original_engine_factory(), control)()
    assert ok is False
    assert "no owner pause" in message
    assert "guarded" in message


def test_close_actually_closes_and_says_what_it_cannot_hold(original_engine_factory, broker):
    """Closing on demand is an owner instruction, not a strategy decision, so it
    works here — and the message does not pretend entries are latched."""
    engine = original_engine_factory()
    engine._tick()
    broker.next_candle()
    engine._tick()
    assert broker.get_pending_orders("XAUUSD", magic=MAGIC)

    ok, message = engine.close_and_pause()
    assert ok is True
    assert broker.get_pending_orders("XAUUSD", magic=MAGIC) == []
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == []
    assert "no pause" in message
    assert engine.running is False, "the loop must be stopped, or the next candle rebuilds the grid"


def test_close_on_an_already_flat_account_says_so(original_engine_factory):
    ok, message = original_engine_factory().close_and_pause()
    assert ok is True
    assert "already reports no positions" in message


def test_close_does_not_claim_success_when_the_broker_cannot_be_read(original_engine_factory, broker):
    engine = original_engine_factory()

    def unreadable(*_a, **_k):
        raise RuntimeError("terminal not answering")

    broker.get_open_positions = unreadable
    ok, message = engine.close_and_pause()
    assert ok is False
    assert "UNKNOWN" in message


def test_stop_reports_what_is_still_open(original_engine_factory, broker):
    engine = original_engine_factory()
    engine._tick()
    broker.next_candle()
    engine._tick()
    assert broker.get_pending_orders("XAUUSD", magic=MAGIC)
    flat, message = engine.stop()
    assert flat is False
    assert "STILL OPEN" in message
    assert "no pause" in message


def test_stop_confirms_a_flat_account(original_engine_factory):
    flat, message = original_engine_factory().stop()
    assert flat is True
    assert "no positions or orders" in message


def test_stop_does_not_claim_flat_when_the_broker_cannot_be_read(original_engine_factory, broker):
    engine = original_engine_factory()

    def unreadable(*_a, **_k):
        raise RuntimeError("terminal not answering")

    broker.get_open_positions = unreadable
    flat, message = engine.stop()
    assert flat is False
    assert "UNKNOWN" in message


def test_the_account_check_is_the_same_in_both_engines(original_engine_factory, engine_factory, broker):
    """Not one of the protections the original is defined by the absence of."""
    account = broker.get_account_info()
    account.trade_mode = "real"
    for engine in (engine_factory(), original_engine_factory()):
        verdict = engine.verify_account(account)
        assert verdict.allowed is False
        assert "refusing rather than trusting" in verdict.reason.lower()


# --------------------------------------------------------- profile selection

def test_the_default_engine_is_the_original_one():
    """The owner asked for the 2 September bot to be what the app runs."""
    from app.config import settings

    assert engine_profiles.DEFAULT_PROFILE == "original"
    assert settings.engine_profile == "original"


@pytest.mark.parametrize("value", [None, "", "nonsense", 7, [], "legacy"])
def test_an_unrecognised_profile_resolves_to_the_GUARDED_one(value):
    """Not to the default. A typo must not select the engine with no gate."""
    assert engine_profiles.normalise(value) == "guarded"


@pytest.mark.parametrize("value,expected", [("original", "original"), (" Original ", "original"),
                                            ("GUARDED", "guarded")])
def test_a_recognised_profile_is_accepted_whatever_its_casing(value, expected):
    assert engine_profiles.normalise(value) == expected


def test_the_registry_describes_both_and_claims_nothing_about_profit():
    text = " ".join(p.summary + " ".join(p.adds) for p in engine_profiles.ALL_PROFILES.values())
    assert "profitab" not in text.lower()
    assert engine_profiles.GUARDED_PROFILE.guarded is True
    assert engine_profiles.ORIGINAL_PROFILE.guarded is False
    assert "2 Sep" in engine_profiles.ORIGINAL_PROFILE.label
