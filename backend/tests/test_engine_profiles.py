"""The toggle between the two engines, and the claims it is allowed to make.

The claim under test is narrow and it is the whole point: **the strategy is the
same on both sides of the switch**. These tests pin that by construction — the
legacy file is diffed against the commit it came from, the adapter is checked
for strategy overrides by name, and both engines are asked to place a grid from
identical settings and compared level by level.

What is deliberately NOT claimed anywhere here: that either profile is
profitable, or that one of them trades better than the other. Nothing in this
repository measures that.
"""

import subprocess
from pathlib import Path

import pytest

from app.engine import profiles as engine_profiles
from app.engine.grid_engine import GridEngine
from app.engine.legacy_adapter import LegacyEngine
from app.engine.legacy_engine import LegacyGridEngine
from tests.conftest import MAGIC, FakeBroker

REPO = Path(__file__).resolve().parents[2]
PINNED_COMMIT = "1116af1"
LEGACY_FILE = REPO / "backend" / "app" / "engine" / "legacy_engine.py"


# --------------------------------------------------------------- provenance

def _git(*args) -> str | None:
    try:
        done = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def test_the_legacy_file_is_the_pinned_commit_with_two_known_edits():
    """The vendored engine must stay the original, byte for byte.

    If this fails, someone edited the legacy engine. That is not a merge
    conflict to resolve by accepting the new side: the file's only purpose is to
    be what the old bot was, and an edited copy compares nothing.
    """
    original = _git("show", f"{PINNED_COMMIT}:backend/app/engine/grid_engine.py")
    if original is None:
        pytest.skip("git history is not available here")
    vendored = LEGACY_FILE.read_text()

    # Edit 1: a provenance header was prepended, ending at the original's first line.
    marker = '"""XAUUSD M1 pending-order grid.'
    assert marker in vendored
    body = vendored[vendored.index(marker):]

    # Edit 2: the class was renamed so both engines can be imported at once.
    assert body.replace("class LegacyGridEngine:", "class GridEngine:") == original


def test_the_header_names_where_the_file_came_from():
    head = LEGACY_FILE.read_text()[:2000]
    assert PINNED_COMMIT in head
    assert "legacy_adapter" in head


# ------------------------------------------------- the adapter's boundaries

#: Methods that decide what is traded, when, at what size, and when it ends.
#: The adapter may not override any of them — if it did, "the legacy engine"
#: would be a third strategy wearing the old one's name.
STRATEGY_METHODS = (
    "_tick",
    "_loop",
    "start",
    "_build_grid",
    "_grid_levels",
    "_completed_grid_loss",
    "_affordability",
    "_entry_gate",
    "_check_risk_limits",
    "_check_daily_target",
    "_close_everything",
    "_basket_pnl",
    "_estimated_exit_cost",
    "_gate_status",
    "_arm_gate",
    "_within_session",
    "_roll_day",
    "_is_hedged",
    "clear_halt",
)


@pytest.mark.parametrize("name", STRATEGY_METHODS)
def test_the_adapter_overrides_no_strategy_method(name):
    assert name not in LegacyEngine.__dict__, (
        f"LegacyEngine overrides {name}, which is a strategy decision. The adapter may add "
        "reporting and owner controls only."
    )
    # And the method really is inherited from the vendored file, not from
    # somewhere else that could be edited instead.
    assert getattr(LegacyEngine, name) is getattr(LegacyGridEngine, name)


def test_the_adapter_overrides_are_all_declared_plumbing():
    """A new override has to be a deliberate decision, not a quiet one."""
    allowed = {
        "__init__", "__module__", "__qualname__", "__doc__", "__dict__", "__weakref__",
        "profile", "_NO_PAUSE",
        "_risk_key", "stop", "pause_entries", "resume_entries", "close_and_pause",
        "verify_account", "_basket_pnl_display", "_day_risk", "_observation_header",
        "_profile_report", "status", "_broadcast",
    }
    assert set(LegacyEngine.__dict__) <= allowed, set(LegacyEngine.__dict__) - allowed


# ------------------------------------------------------ the same grid, twice

def _settings(**over):
    base = dict(
        lot_size=0.02, buy_stop_levels=7, sell_stop_levels=4, grid_distance=0.45,
        basket_take_profit_usd=12.0, basket_stop_loss_usd=40.0, magic_number=MAGIC,
        max_daily_loss_usd=500.0, capital_reserve_percent=50.0,
    )
    base.update(over)
    return base


def test_both_profiles_place_the_identical_grid():
    """Same settings, same price, same orders — side, price and volume."""
    placed = {}
    for key, build, extra in (
        ("guarded", GridEngine, dict(capital_floor_usd=10.0, exit_commission_per_lot=0.0,
                                     slippage_points_per_fill=0.0)),
        ("legacy", LegacyEngine, {}),
    ):
        broker = FakeBroker(price=4000.0, balance=5000.0)
        engine = build(broker=broker, symbol="XAUUSD", mode="demo", **_settings(**extra))
        engine._build_grid()
        placed[key] = sorted(
            (o.order_type.value, round(o.price, 5), o.volume)
            for o in broker.get_pending_orders("XAUUSD", magic=MAGIC)
        )

    assert placed["guarded"] == placed["legacy"]
    assert len(placed["guarded"]) == 11  # 7 buy + 4 sell, both sides unchanged


def test_both_profiles_compute_the_same_level_prices():
    broker = FakeBroker(price=4000.0, balance=5000.0)
    guarded = GridEngine(broker=broker, symbol="XAUUSD", mode="demo",
                         **_settings(capital_floor_usd=10.0, exit_commission_per_lot=0.0,
                                     slippage_points_per_fill=0.0))
    legacy = LegacyEngine(broker=broker, symbol="XAUUSD", mode="demo", **_settings())
    info = broker.get_symbol_info("XAUUSD")
    assert guarded._grid_levels(4000.0, info) == legacy._grid_levels(4000.0, info)


def test_both_profiles_wait_for_the_next_candle_before_the_first_grid():
    """The one gate that is strategy, and is present on both sides."""
    for build, extra in ((GridEngine, dict(capital_floor_usd=10.0, exit_commission_per_lot=0.0,
                                           slippage_points_per_fill=0.0)),
                         (LegacyEngine, {})):
        broker = FakeBroker(price=4000.0, balance=5000.0)
        engine = build(broker=broker, symbol="XAUUSD", mode="demo", **_settings(**extra))
        engine._tick()
        assert broker.get_pending_orders("XAUUSD", magic=MAGIC) == []
        broker.next_candle()
        engine._tick()
        assert len(broker.get_pending_orders("XAUUSD", magic=MAGIC)) == 11


# ------------------------------------------- what the switch actually changes

def test_the_legacy_profile_has_no_capital_floor_gate(legacy_engine_factory, engine_factory, broker):
    """The difference the owner is choosing, demonstrated rather than described.

    With no capital floor set, the guarded engine refuses outright. The legacy
    engine has no such rule — it predates it — so the same settings admit a
    grid. This is not a bug in either: it is the toggle.
    """
    guarded = engine_factory(capital_floor_usd=0.0, basket_stop_loss_usd=40.0,
                             max_daily_loss_usd=500.0)
    allowed, reason = guarded._entry_gate(broker.get_account_info())
    assert allowed is False
    assert "capital floor" in reason

    legacy = legacy_engine_factory(basket_stop_loss_usd=40.0, max_daily_loss_usd=500.0)
    allowed, reason = legacy._entry_gate(broker.get_account_info())
    assert allowed is True, reason


def test_the_legacy_profile_does_not_block_on_unknown_closing_costs(legacy_engine_factory, broker):
    """No closing-cost inputs are supplied, and it admits anyway."""
    legacy = legacy_engine_factory(basket_stop_loss_usd=40.0, max_daily_loss_usd=500.0)
    assert legacy._entry_gate(broker.get_account_info())[0] is True


def test_the_legacy_profile_still_refuses_a_grid_bigger_than_its_budget(legacy_engine_factory, broker):
    """Its own admission is unchanged — not absent, just older."""
    legacy = legacy_engine_factory(basket_stop_loss_usd=1.0, max_daily_loss_usd=500.0,
                                   buy_stop_levels=10, sell_stop_levels=10, lot_size=0.10)
    allowed, reason = legacy._entry_gate(broker.get_account_info())
    assert allowed is False
    assert "locks in about" in reason


def test_the_legacy_profile_reports_what_it_is_not_running(legacy_engine_factory):
    status = legacy_engine_factory(capital_floor_usd=170.0)
    report = status.status()
    assert report["engine_profile"] == "legacy"
    assert "capital_floor_usd" in report["settings_not_applied"]
    assert report["capital_floor_usd"] is None
    assert any("capital floor" in line for line in report["engine_profile_missing"])


def test_the_guarded_profile_names_itself_too(engine_factory):
    assert engine_factory().status()["engine_profile"] == "guarded"


# ------------------------------------------------------------ halt isolation

def test_each_profile_keeps_its_own_halt_record(legacy_engine_factory, engine_factory):
    guarded, legacy = engine_factory(), legacy_engine_factory()
    assert guarded._risk_key() != legacy._risk_key()
    assert legacy._risk_key().startswith("legacyprofile:")

    legacy._halt_reason = "daily loss limit reached"
    legacy._persist_risk_state()

    fresh_guarded = engine_factory()
    assert fresh_guarded._halt_reason is None, "a legacy halt must not appear as a guarded halt"
    fresh_legacy = legacy_engine_factory()
    assert fresh_legacy._halt_reason == "daily loss limit reached"


# --------------------------------------------------------- the status surface

DASHBOARD_KEYS = (
    "running", "mode", "symbol", "connected", "engine_profile", "entries_paused",
    "pause_reason", "halted", "entry_blocked", "entry_block_reason", "day_risk",
    "liquidation_policy", "persistence_error", "trading_window", "in_session",
    "today_net_profit_usd", "snapshot_seq", "observation_account_id",
)


@pytest.mark.parametrize("key", DASHBOARD_KEYS)
def test_both_profiles_answer_every_key_the_dashboard_reads(key, engine_factory, legacy_engine_factory):
    assert key in engine_factory().status()
    assert key in legacy_engine_factory().status()


def test_the_legacy_day_risk_says_unavailable_rather_than_zero(legacy_engine_factory):
    """Zero would be a confident number for a measure this engine never computes."""
    reading = legacy_engine_factory().status()["day_risk"]
    assert reading["available"] is False
    assert reading["marked_result"] is None
    assert "realised" in reading["reason"]


def test_the_legacy_snapshot_sequence_increases(legacy_engine_factory):
    engine = legacy_engine_factory()
    first = engine.status()["snapshot_seq"]
    assert engine.status()["snapshot_seq"] > first


def test_the_legacy_broadcast_carries_the_snapshot_header(legacy_engine_factory, broker):
    seen = []
    engine = legacy_engine_factory(on_update=seen.append)
    engine._tick()
    assert seen, "the legacy engine broadcast nothing"
    payload = seen[-1]
    assert payload["type"] == "tick"
    assert payload["engine_profile"] == "legacy"
    assert isinstance(payload["snapshot_seq"], int)
    assert payload["positions_known"] is True
    assert "grid" in payload


# ------------------------------------------------------------ owner controls

@pytest.mark.parametrize("control", ("pause_entries", "resume_entries"))
def test_legacy_pause_controls_refuse_instead_of_pretending(control, legacy_engine_factory):
    """There is no pause here, and a button that always refuses is better than
    one that appears to hold entries and does not."""
    ok, message = getattr(legacy_engine_factory(), control)()
    assert ok is False
    assert "no owner pause" in message
    assert "guarded" in message


def test_legacy_close_actually_closes_and_says_what_it_cannot_hold(legacy_engine_factory, broker):
    """Closing on demand is an owner instruction, not a strategy decision, so it
    works here — and the message does not pretend entries are latched."""
    engine = legacy_engine_factory()
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


def test_legacy_close_on_an_already_flat_account_says_so(legacy_engine_factory):
    ok, message = legacy_engine_factory().close_and_pause()
    assert ok is True
    assert "already reports no positions" in message


def test_legacy_close_does_not_claim_success_when_the_broker_cannot_be_read(legacy_engine_factory, broker):
    engine = legacy_engine_factory()

    def unreadable(*_a, **_k):
        raise RuntimeError("terminal not answering")

    broker.get_open_positions = unreadable
    ok, message = engine.close_and_pause()
    assert ok is False
    assert "UNKNOWN" in message


def test_legacy_stop_reports_what_is_still_open(legacy_engine_factory, broker):
    engine = legacy_engine_factory()
    engine._tick()          # arms the gate on this candle
    broker.next_candle()
    engine._tick()          # places the grid
    assert broker.get_pending_orders("XAUUSD", magic=MAGIC)
    flat, message = engine.stop()
    assert flat is False
    assert "STILL OPEN" in message
    assert "no pause" in message


def test_legacy_stop_confirms_a_flat_account(legacy_engine_factory):
    flat, message = legacy_engine_factory().stop()
    assert flat is True
    assert "no positions or orders" in message


def test_legacy_stop_does_not_claim_flat_when_the_broker_cannot_be_read(legacy_engine_factory, broker):
    engine = legacy_engine_factory()

    def unreadable(*_a, **_k):
        raise RuntimeError("terminal not answering")

    broker.get_open_positions = unreadable
    flat, message = engine.stop()
    assert flat is False
    assert "UNKNOWN" in message


def test_the_account_check_is_the_same_in_both_profiles(legacy_engine_factory, engine_factory, broker):
    """Not one of the protections the legacy profile is defined by the absence of."""
    account = broker.get_account_info()
    account.trade_mode = "real"
    for engine in (engine_factory(), legacy_engine_factory()):
        verdict = engine.verify_account(account)
        assert verdict.allowed is False
        assert "refusing rather than trusting" in verdict.reason.lower()


# --------------------------------------------------------- profile selection

@pytest.mark.parametrize("value", [None, "", "GUARDED ", "nonsense", 7, "Legacy"])
def test_an_unrecognised_profile_resolves_to_the_guarded_one(value):
    resolved = engine_profiles.normalise(value)
    assert resolved in engine_profiles.ALL_PROFILES
    if value == "Legacy":
        assert resolved == "legacy"
    else:
        assert resolved == "guarded"


def test_the_registry_describes_both_and_claims_nothing_about_profit():
    text = " ".join(p.summary + " ".join(p.adds) for p in engine_profiles.ALL_PROFILES.values())
    assert "profitab" not in text.lower()
    assert engine_profiles.GUARDED_PROFILE.guarded is True
    assert engine_profiles.LEGACY_PROFILE.guarded is False
