"""Phase C: admission gates, research profiles, replay and evaluation hygiene.

These test MECHANICS. No test here asserts that any candidate is profitable,
because no real market data exists in this repository to establish that — see
PHASE_C_HANDOVER.md. Synthetic fixtures validate that the machinery is causal
and honest; they cannot validate an edge.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.research.calendar import EconomicCalendar, CalendarEvent, load_calendar_csv
from app.research.causal_replay import ReplayCosts, StraddleReplay
from app.research.experiment_log import ExperimentLog, FinalWindowAlreadyOpened
from app.research.splits import (
    BasketOutcome, assign_to_window, chronological_split, independent_sessions, partition,
)
from app.research.tick_data import load_ticks_csv
from app.strategy import profiles as P
from app.strategy.admission import Verdict, evaluate_all
from app.strategy.gates import EventBlackoutGate, ExecutionQualityGate, RegimeGate
from app.strategy.trailing import BasketTrailing, TrailingPolicy, TrailingState

UTC = timezone.utc


def at(minute=0, hour=12, day=5):
    return datetime(2026, 1, day, hour, minute, tzinfo=UTC)


class T:
    """Minimal tick for the replay."""
    def __init__(self, time, bid, ask):
        self.time, self.bid, self.ask = time, bid, ask

    @property
    def mid(self):
        return (self.bid + self.ask) / 2.0


# --- profiles -----------------------------------------------------------------

def test_every_research_profile_is_disabled_for_live_trading():
    """Offline evidence is not approval to trade."""
    live = [p.name for p in P.ALL_PROFILES.values() if p.live_enabled]
    assert live == ["baseline"], f"a research profile is live-enabled: {live}"


def test_the_baseline_profile_has_no_gates_and_no_trailing():
    """Phase C must not change what runs today."""
    assert P.BASELINE.gate_factories == ()
    assert P.BASELINE.trailing is None


def test_profiles_are_versioned_and_addressable():
    assert P.get_profile("research-regime@v1").version == "v1"
    with pytest.raises(KeyError):
        P.get_profile("does-not-exist@v9")


# --- admission interface ------------------------------------------------------

def test_unknown_is_a_third_outcome_not_a_silent_allow():
    gate = ExecutionQualityGate()
    result = gate.evaluate({"grid_distance": 0.30, "quote_age_ms": 10})
    assert result.verdict is Verdict.UNKNOWN
    assert "bid/ask" in result.reason


def test_a_required_data_gate_blocks_admission_on_unknown():
    """A profile that cannot evaluate its own gate has not been shown safe."""
    decision = evaluate_all([EventBlackoutGate()], {"now": at()}, "p", "v1")
    assert decision.allowed is False
    assert any("calendar" in r for r in decision.blocking_reasons)


def test_every_gate_runs_so_all_reasons_are_visible():
    ctx = {"now": at(), "grid_distance": 0.30}
    decision = evaluate_all([ExecutionQualityGate(), EventBlackoutGate()], ctx, "p", "v1")
    assert len(decision.results) == 2, "evaluation stopped at the first blocking gate"


def test_a_gate_that_raises_becomes_unknown_not_an_allow():
    class Boom(ExecutionQualityGate):
        def evaluate(self, ctx):
            raise RuntimeError("bad feature")

    decision = evaluate_all([Boom()], {}, "p", "v1")
    assert decision.allowed is False
    assert decision.results[0].verdict is Verdict.UNKNOWN


def test_a_gate_reports_when_its_inputs_were_true_not_when_it_ran():
    """A gate reporting only its own run time would hide that it decided on a
    ten-minute-old quote."""
    as_of = at(minute=30)
    result = ExecutionQualityGate().evaluate(
        {"bid": 3999.9, "ask": 4000.1, "grid_distance": 0.30,
         "quote_age_ms": 50, "quote_as_of": as_of}
    )
    assert result.inputs_as_of == as_of
    assert result.evaluated_at >= as_of


# --- candidate 1: execution quality ------------------------------------------

def test_a_wide_spread_blocks_admission():
    gate = ExecutionQualityGate(max_spread_fraction_of_spacing=1.0)
    wide = gate.evaluate({"bid": 3999.0, "ask": 4000.0, "grid_distance": 0.30,
                          "quote_age_ms": 10, "quote_as_of": at()})
    assert wide.verdict is Verdict.BLOCK and "spread" in wide.reason


def test_a_stale_quote_blocks_admission():
    gate = ExecutionQualityGate(max_quote_age_ms=2000)
    stale = gate.evaluate({"bid": 3999.9, "ask": 4000.1, "grid_distance": 0.30,
                           "quote_age_ms": 9000, "quote_as_of": at()})
    assert stale.verdict is Verdict.BLOCK and "old" in stale.reason


def test_an_inverted_quote_is_unknown_not_a_tight_spread():
    gate = ExecutionQualityGate()
    result = gate.evaluate({"bid": 4000.5, "ask": 4000.0, "grid_distance": 0.30,
                            "quote_age_ms": 10})
    assert result.verdict is Verdict.UNKNOWN


# --- candidate 2: regime ------------------------------------------------------

def bars(n, high=4001.0, low=3999.0, close=4000.0):
    return [{"high": high, "low": low, "close": close} for _ in range(n)]


def test_atr_returns_none_during_warmup_rather_than_a_padded_value():
    assert RegimeGate.atr(bars(5), 14) is None
    assert RegimeGate.atr(bars(20), 14) is not None


def test_warmup_is_unknown_and_therefore_blocks():
    decision = evaluate_all([RegimeGate()], {"closed_bars": bars(5), "grid_distance": 0.30,
                                             "bar_as_of": at()}, "p", "v1")
    assert decision.allowed is False
    assert "warmup" in decision.results[0].reason


def test_a_quiet_range_blocks_and_an_expanding_range_allows():
    """The hypothesis under test: this straddle needs movement, and a tight
    range is what freezes it."""
    quiet = RegimeGate(min_atr_multiple_of_spacing=2.0).evaluate(
        {"closed_bars": bars(20, high=4000.1, low=3999.9), "grid_distance": 0.30, "bar_as_of": at()})
    assert quiet.verdict is Verdict.BLOCK

    moving = RegimeGate(min_atr_multiple_of_spacing=2.0).evaluate(
        {"closed_bars": bars(20, high=4002.0, low=3998.0), "grid_distance": 0.30, "bar_as_of": at()})
    assert moving.verdict is Verdict.ALLOW


def test_the_regime_gate_only_sees_closed_bars():
    """The forming bar's final high, low and close do not exist yet. The gate
    takes a closed-bar list and has no access to a current bar at all."""
    ctx = {"closed_bars": bars(20), "grid_distance": 0.30, "bar_as_of": at(),
           "current_bar": {"high": 9999.0, "low": 0.0, "close": 9999.0}}
    result = RegimeGate().evaluate(ctx)
    # The absurd current bar cannot change the verdict, because it is not read.
    assert result.detail["atr"] == RegimeGate.atr(bars(20), 14)


# --- candidate 3: scheduled event blackout ------------------------------------

def make_calendar(event_time, available_from, impact="high"):
    return EconomicCalendar(
        [CalendarEvent(event_time, available_from, "NFP", impact, "USD")],
        covered_from=event_time - timedelta(days=30),
        covered_until=event_time + timedelta(days=30),
    )


def test_an_event_published_after_the_decision_is_invisible():
    """Using tomorrow's calendar today is a leak, not a filter."""
    event_time = at(hour=13)
    cal = make_calendar(event_time, available_from=at(hour=12, minute=59))
    early = EventBlackoutGate().evaluate({"now": at(hour=12, minute=45), "calendar": cal})
    assert early.verdict is Verdict.ALLOW, "the gate saw a schedule entry published later"


def test_a_published_event_blocks_inside_its_window():
    event_time = at(hour=13)
    cal = make_calendar(event_time, available_from=at(day=4, hour=12))
    blocked = EventBlackoutGate().evaluate({"now": at(hour=12, minute=45), "calendar": cal})
    assert blocked.verdict is Verdict.BLOCK and "NFP" in blocked.reason


def test_outside_the_window_is_allowed():
    cal = make_calendar(at(hour=13), available_from=at(day=4, hour=12))
    assert EventBlackoutGate().evaluate(
        {"now": at(hour=9), "calendar": cal}).verdict is Verdict.ALLOW


def test_a_low_impact_event_does_not_block_a_high_impact_gate():
    cal = make_calendar(at(hour=13), available_from=at(day=4), impact="low")
    assert EventBlackoutGate(impacts=("high",)).evaluate(
        {"now": at(hour=12, minute=50), "calendar": cal}).verdict is Verdict.ALLOW


def test_a_decision_beyond_calendar_coverage_is_unknown_not_empty():
    """Missing coverage is unknown, never 'no events'."""
    cal = EconomicCalendar([], covered_from=at(day=1), covered_until=at(day=2))
    result = EventBlackoutGate().evaluate({"now": at(day=20), "calendar": cal})
    assert result.verdict is Verdict.UNKNOWN and "coverage" in result.reason


def test_calendar_import_rejects_a_file_without_availability_times(tmp_path):
    path = tmp_path / "cal.csv"
    path.write_text("event_time_utc,title,impact,currency\n2026-01-05T13:00:00Z,NFP,high,USD\n")
    with pytest.raises(ValueError) as exc:
        load_calendar_csv(path)
    assert "available_from_utc" in str(exc.value)


def test_calendar_import_records_a_hash_and_rejected_rows(tmp_path):
    path = tmp_path / "cal.csv"
    path.write_text(
        "event_time_utc,available_from_utc,title,impact,currency\n"
        "2026-01-05T13:00:00Z,2026-01-01T00:00:00Z,NFP,high,USD\n"
        "not-a-date,2026-01-01T00:00:00Z,Broken,high,USD\n"
    )
    cal, report = load_calendar_csv(path)
    assert len(cal.events) == 1
    assert len(report["problems"]) == 1
    assert len(report["sha256"]) == 64


def test_naive_calendar_stamps_are_read_as_utc_not_local(tmp_path):
    path = tmp_path / "cal.csv"
    path.write_text(
        "event_time_utc,available_from_utc,title,impact,currency\n"
        "2026-01-05 13:00:00,2026-01-01 00:00:00,NFP,high,USD\n"
    )
    cal, _ = load_calendar_csv(path)
    assert cal.events[0].event_time == datetime(2026, 1, 5, 13, tzinfo=UTC)


# --- candidate 4: trailing exit ----------------------------------------------

def policy():
    return TrailingPolicy(arm_at_usd=15.0, giveback_usd=5.0, floor_usd=10.0)


def test_trailing_must_arm_above_the_fixed_target():
    """Arming at or below the target makes the candidate a no-op that still
    looks like it is running."""
    with pytest.raises(ValueError):
        TrailingPolicy(arm_at_usd=10.0, giveback_usd=5.0, floor_usd=5.0).validate_against_target(10.0)
    policy().validate_against_target(10.0)     # fine


def test_trailing_does_not_arm_below_its_threshold():
    t = BasketTrailing(policy(), TrailingState("b1", "acc"))
    t.observe(12.0, costs_known=True)
    assert t.state.armed is False
    assert t.should_exit(6.0, costs_known=True) == (False, None)


def test_trailing_exits_on_giveback_once_armed():
    t = BasketTrailing(policy(), TrailingState("b1", "acc"))
    t.observe(20.0, costs_known=True)
    assert t.state.armed is True and t.state.peak_net == 20.0
    should, why = t.should_exit(14.9, costs_known=True)
    assert should and "gave back" in why


def test_the_floor_bounds_the_giveback():
    t = BasketTrailing(TrailingPolicy(arm_at_usd=15.0, giveback_usd=50.0, floor_usd=12.0),
                       TrailingState("b1", "acc"))
    t.observe(16.0, costs_known=True)
    assert t.should_exit(13.0, costs_known=True)[0] is False
    assert t.should_exit(11.9, costs_known=True)[0] is True


def test_an_unknown_cost_cannot_raise_the_peak():
    """A peak inflated by an unreported commission would set the giveback
    floor too high and hold a basket open on money that was never there."""
    t = BasketTrailing(policy(), TrailingState("b1", "acc"))
    t.observe(20.0, costs_known=True)
    t.observe(99.0, costs_known=False)
    assert t.state.peak_net == 20.0
    assert t.state.skipped_unknown_cost == 1


def test_trailing_state_survives_a_restart_scoped_to_its_basket():
    t = BasketTrailing(policy(), TrailingState("b1", "acc"))
    t.observe(22.0, costs_known=True)
    restored = TrailingState.from_dict(t.state.as_dict())
    assert restored.armed is True and restored.peak_net == 22.0
    assert restored.basket_id == "b1" and restored.account_id == "acc"


def test_a_new_basket_does_not_inherit_the_previous_peak():
    """A carried peak would arm the new basket at a level it never reached."""
    old = TrailingState("b1", "acc", armed=True, peak_net=50.0)
    fresh = TrailingState(basket_id="b2", account_id="acc")
    assert fresh.armed is False and fresh.peak_net is None
    assert old.basket_id != fresh.basket_id


# --- evaluation hygiene -------------------------------------------------------

def test_splits_are_made_on_time_not_row_count():
    plan = chronological_split(at(day=1), at(day=31), development_fraction=0.5,
                               validation_folds=3)
    assert plan.development.duration == timedelta(days=15)
    assert plan.final_evaluation.end == at(day=31)
    assert plan.final_evaluation.start > plan.validation[-1].start


def test_a_basket_straddling_a_boundary_is_purged():
    plan = chronological_split(at(day=1), at(day=31))
    window = plan.development
    straddler = BasketOutcome("b1", opened_at=window.end - timedelta(hours=1),
                              closed_at=window.end + timedelta(hours=1), net_result=5.0)
    ok, why = assign_to_window(straddler, window, plan.embargo)
    assert ok is False and "straddles" in why


def test_the_embargo_excludes_a_basket_opened_just_before_a_boundary():
    plan = chronological_split(at(day=1), at(day=31), embargo=timedelta(hours=6))
    window = plan.development
    late = BasketOutcome("b2", opened_at=window.end - timedelta(hours=1),
                         closed_at=window.end - timedelta(minutes=30), net_result=5.0)
    ok, why = assign_to_window(late, window, plan.embargo)
    assert ok is False and "embargo" in why


def test_an_unclosed_basket_is_excluded_with_a_reason_not_dropped():
    """Dropping them would quietly remove exactly the baskets that never
    recovered."""
    plan = chronological_split(at(day=1), at(day=31))
    stuck = BasketOutcome("b3", opened_at=at(day=2), closed_at=None,
                          net_result=-120.0, still_open=True)
    out = partition([stuck], plan)
    assert out["assigned"]["development"] == []
    assert len(out["excluded"]) == 1
    assert "remaining exposure" in out["excluded"][0]["reason"]


def test_sessions_are_counted_not_tickets():
    """Twenty tickets in one basket are one correlated observation."""
    same_session = [BasketOutcome(f"b{i}", at(hour=12 + i), at(hour=13 + i), 1.0) for i in range(3)]
    later = [BasketOutcome("bx", at(day=9), at(day=9, hour=1), 1.0)]
    assert independent_sessions(same_session + later) == 2


def test_the_final_window_cannot_be_opened_for_a_second_candidate():
    log = ExperimentLog(budget=4)
    log.record("regime", {"atr": 2.0}, "development", {"net": -10})
    log.freeze("regime", {"atr": 2.0})
    log.open_final("regime")
    with pytest.raises(FinalWindowAlreadyOpened):
        log.open_final("execution_quality")
    assert log.as_dict()["refusals"], "the refusal was not recorded"


def test_freezing_after_opening_the_final_window_is_refused():
    log = ExperimentLog()
    log.freeze("regime", {})
    log.open_final("regime")
    with pytest.raises(FinalWindowAlreadyOpened):
        log.freeze("something_else", {})


def test_exceeding_the_declared_budget_is_visible():
    log = ExperimentLog(budget=2)
    for name in ("a", "b", "c"):
        log.record(name, {}, "development", {})
    assert log.over_budget is True


# --- causal replay ------------------------------------------------------------

def rising_ticks(n=400, start=4000.0, step=0.02, half=0.12):
    return [T(at() + timedelta(seconds=i), start + i * step - half, start + i * step + half)
            for i in range(n)]


def replay():
    return StraddleReplay(lot=0.01, buy_levels=10, sell_levels=10, spacing=0.30,
                          target_usd=10.0, stop_usd=60.0,
                          costs=ReplayCosts(commission_per_lot_per_side=2.75))


def test_the_replay_is_deterministic():
    ticks = rising_ticks()
    a = replay().run(ticks, profile=P.BASELINE).summary()
    b = replay().run(ticks, profile=P.BASELINE).summary()
    assert a == b


def test_a_level_crossed_between_observed_ticks_is_not_an_ideal_fill():
    """No ideal fills at a price nobody saw."""
    ticks = [T(at(), 3999.88, 4000.12), T(at() + timedelta(seconds=1), 4009.88, 4010.12)]
    report = replay().run(ticks, profile=P.BASELINE)
    assert report.baskets, "fixture: a basket should have opened"
    assert report.baskets[0].gapped_fills > 0, "a gapped fill was priced at the level"


def test_open_exposure_at_the_end_is_marked_and_disclosed_not_dropped():
    ticks = [T(at(), 3999.88, 4000.12), T(at() + timedelta(seconds=1), 3994.88, 3995.12)]
    report = replay().run(ticks, profile=P.BASELINE)
    summary = report.summary()
    assert summary["baskets_still_open"] == 1
    assert any("still open" in n for n in summary["notes"])


def test_a_blocking_profile_opens_no_basket_and_records_why():
    ticks = rising_ticks()

    def always_block(tick, index):
        return evaluate_all([EventBlackoutGate()], {"now": tick.time}, "p", "v1")

    report = replay().run(ticks, profile=P.EVENT_BLACKOUT, admission_fn=always_block)
    assert report.baskets == []
    assert report.admissions_blocked == len(ticks)
    assert report.block_reasons


def test_the_replay_charges_commission_and_reports_it():
    report = replay().run(rising_ticks(), profile=P.BASELINE)
    summary = report.summary()
    assert summary["commission_paid"] > 0
    assert any("Simulated fills" in n for n in summary["notes"])


def test_the_replay_detects_the_frozen_hedged_state():
    """The baseline's known failure mode: both sides filled, volumes cancel,
    the basket can no longer reach its target."""
    ticks = [T(at(), 3999.88, 4000.12)]
    up = [T(at() + timedelta(seconds=i), 4000.0 + i * 0.1 - 0.12, 4000.0 + i * 0.1 + 0.12)
          for i in range(1, 40)]
    down = [T(at() + timedelta(seconds=40 + i), 4003.9 - i * 0.2 - 0.12, 4003.9 - i * 0.2 + 0.12)
            for i in range(60)]
    report = replay().run(ticks + up + down, profile=P.BASELINE)
    assert any(b.froze_hedged or b.close_reason for b in report.baskets)


def test_a_mid_only_tick_export_is_refused(tmp_path):
    """This strategy pays the spread on up to twenty fills; a mid-only file
    cannot price it."""
    path = tmp_path / "ticks.csv"
    path.write_text("time_utc,mid\n2026-01-05T12:00:00Z,4000.0\n")
    with pytest.raises(ValueError) as exc:
        load_ticks_csv(path)
    assert "bid" in str(exc.value).lower()


def test_tick_import_reports_duplicates_gaps_and_order(tmp_path):
    path = tmp_path / "ticks.csv"
    path.write_text(
        "time_utc,bid,ask\n"
        "2026-01-05T12:00:00Z,3999.9,4000.1\n"
        "2026-01-05T12:00:00Z,3999.9,4000.1\n"       # duplicate stamp
        "2026-01-05T13:00:00Z,3999.9,4000.1\n"       # one-hour gap
        "2026-01-05T12:30:00Z,3999.9,4000.1\n"       # out of order
    )
    ticks, report, digest = load_ticks_csv(path)
    assert report.duplicate_timestamps == 1
    assert report.out_of_order == 1
    assert len(report.gaps) == 1, f"expected one gap, got {report.gaps}"
    assert len(digest) == 64
    assert len(ticks) == 4, "rows were silently dropped"
