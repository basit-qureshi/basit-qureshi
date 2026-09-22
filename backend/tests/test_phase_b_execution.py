"""Phase B: execution separation, and what it must not break.

Behavioural tests use deterministic fixtures — no sleeps, no wall clock, no
timing assertions. Timing claims belong in tests/bench/bench_execution.py,
which is a separate repeatable benchmark, because a test that asserts on
milliseconds fails on a busy machine and teaches everyone to ignore it.
"""

import threading
import time

import pytest

from app.backtest.execution_replay import CostModel, SimPosition, Tick, compare_exit_only, run_exit_only
from app.engine.broker_owner import PROTECTIVE, REPORTING, BrokerOwner, SkipReporting
from app.engine.instrumentation import QuoteObservation, Recorder, Sample, summarize
from tests.conftest import MAGIC


def orders(broker):
    return broker.get_pending_orders("XAUUSD", magic=MAGIC)


# --- instrumentation honesty --------------------------------------------------

def test_slow_and_failed_samples_are_kept_not_dropped():
    """Dropping a timed-out call because it has no clean duration is how a p99
    comes out reassuring."""
    samples = [Sample("x", 5.0) for _ in range(99)] + [Sample("x", 9000.0, censored=True)]
    stats = summarize("x", samples)
    assert stats.count == 100
    assert stats.censored == 1
    assert stats.maximum == 9000.0
    assert stats.p99 is not None, "100 samples supports a p99"


def test_percentiles_are_withheld_when_the_sample_is_too_small():
    """A 'p99' over 12 observations is the maximum wearing a different label."""
    stats = summarize("x", [Sample("x", float(i)) for i in range(12)])
    assert stats.median is not None
    assert stats.p95 is None and stats.p99 is None


def test_the_recorder_drops_the_oldest_sample_not_the_slowest():
    """Trimming by value would bias every percentile downward."""
    rec = Recorder(cap=3)
    for ms in (1.0, 9999.0, 2.0, 3.0):
        rec.record("x", ms)
    assert rec.stats("x").maximum == 9999.0


def test_quote_age_uses_the_monotonic_clock_and_is_not_called_latency():
    q = QuoteObservation(price=4000.0, broker_time="2026-01-05T09:00:00")
    assert q.local_age_ms(q.observed_monotonic_ms + 250.0) == 250.0
    # The two-clock figure is a separate, explicitly nullable field; nothing
    # subtracts a broker stamp from a local clock to produce a latency number.
    assert q.age_seconds_two_clock is None


def test_a_missing_quote_is_its_own_state():
    """symbol_info_tick returns the last tick and can return None. Missing is
    not zero and is not a stale price quietly reused."""
    q = QuoteObservation(price=None, broker_time=None, missing=True)
    assert q.is_stale(limit_ms=10_000) is True


# --- broker owner: priority, stalls, honesty ---------------------------------

def test_reporting_stands_aside_for_waiting_protective_work(broker):
    owner = BrokerOwner(broker)
    owner._protective_waiting.set()          # a protective caller is queued
    with pytest.raises(SkipReporting):
        owner.call("history", lambda: "expensive", priority=REPORTING)
    assert owner.health.reporting_skipped == 1


def test_a_skipped_reporting_read_is_not_an_empty_result(broker):
    """A caller that cannot tell 'skipped' from 'nothing there' will
    eventually render a skip as a flat account."""
    owner = BrokerOwner(broker)
    owner._protective_waiting.set()
    try:
        owner.call("pendings", lambda: [], priority=REPORTING)
    except SkipReporting as exc:
        assert "yielded" in str(exc)
    else:
        pytest.fail("a skipped read returned a value instead of saying it was skipped")


def test_a_stalled_broker_call_reports_blocked_and_never_duplicates(broker):
    """The honest state is 'we cannot tell'. Firing a second request because
    the first is late risks two live orders for one decision."""
    owner = BrokerOwner(broker, stall_after_ms=50.0)
    release = threading.Event()
    calls = []

    def stuck():
        calls.append(1)
        release.wait(timeout=5)
        return "late"

    worker = threading.Thread(target=lambda: owner.call("positions", stuck, priority=PROTECTIVE))
    worker.start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not owner.snapshot_health().blocked:
            time.sleep(0.01)
        health = owner.snapshot_health()
        assert health.blocked is True
        assert health.in_flight == "positions"
        assert len(calls) == 1, "a second request was issued while the first was in flight"
    finally:
        release.set()
        worker.join(timeout=5)

    assert owner.snapshot_health().blocked is False, "the block must clear once the call returns"


def test_a_blocked_owner_refuses_new_exposure(broker, engine_factory):
    e = engine_factory(basket_stop_loss_usd=60.0)
    e._tick()

    class AlwaysBlocked:
        def snapshot_health(self):
            from app.engine.broker_owner import OwnerHealth
            return OwnerHealth(blocked=True, in_flight="positions", in_flight_ms=9000.0)

    e._owner = AlwaysBlocked()
    allowed, reason = e._entry_gate(broker.get_account_info())
    assert allowed is False
    assert "BLOCKED" in reason and "No competing request" in reason


def test_a_failed_broker_call_is_recorded_not_swallowed(broker):
    owner = BrokerOwner(broker)
    with pytest.raises(RuntimeError):
        owner.call("positions", lambda: (_ for _ in ()).throw(RuntimeError("link down")),
                   priority=PROTECTIVE)
    assert owner.recorder.stats("broker.positions").failed == 1
    assert "link down" in (owner.health.last_error or "")


# --- the split itself ---------------------------------------------------------

def test_the_protective_path_does_not_read_candles_or_sweep_history(broker, engine_factory):
    """Chart data and a full history sweep cannot change whether a stop should
    fire, so neither may run in front of the check that fires it."""
    e = engine_factory(basket_stop_loss_usd=60.0)
    e._tick()
    broker.next_candle()
    e._tick()

    seen = []
    real_candles = broker.get_candles
    broker.get_candles = lambda *a, **k: (seen.append("candles"), real_candles(*a, **k))[1]
    broker.history_records = lambda *a, **k: (seen.append("history"), [])[1]
    broker.acknowledge_history = lambda: None

    e._protective_tick()
    assert seen == [], f"the protective path did {seen}"


def test_slow_reporting_does_not_delay_available_protective_work(broker, engine_factory):
    """The dependency inversion this phase exists to fix."""
    e = engine_factory(basket_take_profit_usd=10_000.0, basket_stop_loss_usd=60.0,
                       max_daily_loss_usd=10_000.0, capital_floor_usd=50.0)
    e._tick()
    broker.next_candle()
    e._tick()
    broker.price += 4.0
    broker.next_candle()
    e._tick()
    assert broker.get_open_positions("XAUUSD", magic=MAGIC), "fixture: the grid must have filled"
    for o in list(orders(broker)):
        broker.cancel_pending_order(o.ticket)

    # Reporting is now impossibly slow; protection must not be behind it.
    def never_returns(*a, **k):
        raise AssertionError("the protective path waited on a history sweep")

    broker.history_records = never_returns
    broker.get_candles = never_returns

    broker.price -= 30.0                      # through the stop
    e._protective_tick()
    # A confirmed close now retires its intent, so the evidence that the stop
    # fired is the counted basket and the latch it left behind.
    assert e._baskets_stopped == 1, "the stop did not fire without reporting"
    assert e._entries_paused, "a loss exit must latch entries"
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == []


def test_a_disconnected_ui_does_not_delay_protection(broker, engine_factory):
    e = engine_factory(basket_take_profit_usd=10_000.0, basket_stop_loss_usd=60.0,
                       max_daily_loss_usd=10_000.0)
    e.on_update = lambda payload: (_ for _ in ()).throw(RuntimeError("websocket gone"))
    e._tick()
    broker.next_candle()
    e._tick()
    broker.price += 4.0
    broker.next_candle()
    e._tick()
    assert broker.get_open_positions("XAUUSD", magic=MAGIC), "fixture: the grid must have filled"
    for o in list(orders(broker)):
        broker.cancel_pending_order(o.ticket)
    broker.price -= 30.0
    e._protective_tick()                      # must not raise, must still close
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == []


def test_rapid_repeated_quotes_cannot_duplicate_a_close_or_a_replacement(broker, engine_factory):
    """Running the protective path many times between candles must not open a
    second intent, count a basket twice or place a second grid."""
    e = engine_factory(basket_take_profit_usd=1.0, basket_stop_loss_usd=60.0,
                       max_daily_loss_usd=10_000.0)
    e._tick()
    broker.next_candle()
    e._tick()
    broker.price += 4.0
    for _ in range(25):
        e._protective_tick()
    assert e._baskets_won <= 1, "repeated quotes counted the basket more than once"
    assert len([o for o in orders(broker)]) in (0, 20), "a partial second grid appeared"


def test_the_protective_cadence_is_independent_of_the_candle(broker, engine_factory):
    """Protection must not wait for a new bar to notice a breach."""
    e = engine_factory(basket_take_profit_usd=10_000.0, basket_stop_loss_usd=60.0,
                       max_daily_loss_usd=10_000.0)
    e._tick()
    broker.next_candle()
    e._tick()
    broker.price += 4.0
    broker.next_candle()
    e._tick()
    assert broker.get_open_positions("XAUUSD", magic=MAGIC), "fixture: the grid must have filled"
    for o in list(orders(broker)):
        broker.cancel_pending_order(o.ticket)

    candle_before = broker.candle_time
    broker.price -= 30.0
    e._protective_tick()                      # same candle
    assert broker.candle_time == candle_before
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == []


# --- Phase A behaviour must survive the refactor ------------------------------

def test_a_loss_exit_still_cannot_auto_restart(broker, engine_factory):
    e = engine_factory(basket_take_profit_usd=10_000.0, basket_stop_loss_usd=60.0,
                       max_daily_loss_usd=10_000.0)
    e._tick()
    broker.next_candle()
    e._tick()
    broker.price += 4.0
    broker.next_candle()
    e._tick()
    assert broker.get_open_positions("XAUUSD", magic=MAGIC), "fixture: the grid must have filled"
    for o in list(orders(broker)):
        broker.cancel_pending_order(o.ticket)
    broker.price -= 30.0
    e._protective_tick()

    for _ in range(5):
        broker.next_candle()
        e._tick()
    assert orders(broker) == [], "a loss exit restarted on its own after the refactor"
    assert e._entries_paused is True


def test_profitable_replacement_still_happens_on_the_same_candle(broker, engine_factory):
    e = engine_factory(basket_take_profit_usd=1.0, basket_stop_loss_usd=60.0,
                       max_daily_loss_usd=10_000.0)
    e._tick()
    broker.next_candle()
    e._tick()
    candle = broker.candle_time
    broker.price += 4.0
    e._tick()
    assert broker.candle_time == candle
    assert e._baskets_won == 1
    assert len(orders(broker)) == 20


def test_account_checks_and_manual_ownership_survive_the_refactor(broker, engine_factory):
    e = engine_factory(basket_stop_loss_usd=60.0)
    e._tick()
    manual = broker.open_position("BUY", 4000.0, magic=111111)

    broker.trade_mode = "real"
    assert e.verify_account(broker.get_account_info()).allowed is False

    broker.trade_mode = "demo"
    broker.next_candle()
    e._tick()
    broker.price += 4.0
    e._protective_tick()
    ok, _ = e.close_and_pause()
    assert ok
    assert manual.ticket in broker.positions, "a manual position was closed by the refactored path"


def test_restart_still_recovers_a_closing_basket(broker, engine_factory, monkeypatch):
    e = engine_factory(basket_take_profit_usd=10_000.0, basket_stop_loss_usd=60.0,
                       max_daily_loss_usd=10_000.0)
    e._tick()
    broker.next_candle()
    e._tick()
    broker.price += 4.0
    broker.next_candle()
    e._tick()
    assert broker.get_open_positions("XAUUSD", magic=MAGIC), "fixture: the grid must have filled"
    for o in list(orders(broker)):
        broker.cancel_pending_order(o.ticket)
    monkeypatch.setattr(broker, "close_position",
                        lambda t: (_ for _ in ()).throw(RuntimeError("no")))
    broker.price -= 30.0
    e._protective_tick()
    assert e._close_intent is not None

    monkeypatch.undo()
    fresh = engine_factory(basket_take_profit_usd=10_000.0, basket_stop_loss_usd=60.0,
                           max_daily_loss_usd=10_000.0)
    assert fresh._close_intent is not None
    fresh._protective_tick()
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == []


def test_a_pending_cancellation_can_race_a_fill_and_the_fill_is_managed(broker, engine_factory):
    """A stop can fill in the moment between reading the book and cancelling
    it. The resulting position must be discovered before any replacement."""
    e = engine_factory(basket_stop_loss_usd=60.0)
    e._tick()
    broker.next_candle()
    e._tick()
    ok, _ = e.pause_entries()
    assert ok
    broker.open_position("BUY", 4001.0)       # the raced fill
    e._protective_tick()
    assert broker.get_open_positions("XAUUSD", magic=MAGIC), "the raced fill was lost"
    allowed, reason = e._entry_gate(broker.get_account_info())
    assert allowed is False and "PAUSED" in reason


# --- replay: does the faster cadence actually help? ---------------------------

def descending_ticks(count=120, start=4000.0, step=0.10, unseen_every=0):
    ticks = []
    for i in range(count):
        mid = start - i * step
        ticks.append(Tick(index=i, bid=mid - 0.12, ask=mid + 0.12,
                          observable=not (unseen_every and i % unseen_every == 0)))
    return ticks


def ten_longs():
    return [SimPosition(side="BUY", volume=0.01, open_price=4000.0) for _ in range(10)]


def test_replay_is_deterministic_and_identical_inputs_give_identical_outputs():
    ticks = descending_ticks()
    a = run_exit_only(ticks, ten_longs(), 15.0, policy="p", decision_every=1)
    b = run_exit_only(ticks, ten_longs(), 15.0, policy="p", decision_every=1)
    assert a.as_dict() == b.as_dict()


def test_a_faster_cadence_reduces_limit_overshoot_in_this_fixture():
    """A stated, falsifiable claim about MECHANICS — not about profitability."""
    ticks = descending_ticks()
    out = compare_exit_only(ticks, ten_longs, 15.0, old_decision_every=20, new_decision_every=2)
    assert out["new"]["limit_overshoot"] <= out["old"]["limit_overshoot"]
    assert any("not evidence of profitability" in c for c in out["caveats"])


def test_unobserved_ticks_are_reported_rather_than_treated_as_captured():
    """Polling sees the last tick, not every tick. A profit that existed only
    between two samples must not be claimed."""
    ticks = descending_ticks(unseen_every=3)
    result = run_exit_only(ticks, ten_longs(), 15.0, policy="p", decision_every=1)
    assert any("never observed" in n for n in result.notes)


def test_a_latched_intent_keeps_closing_through_a_reversal_in_replay():
    ticks = [Tick(index=i, bid=4000.0 - i * 0.5 - 0.12, ask=4000.0 - i * 0.5 + 0.12) for i in range(8)]
    ticks += [Tick(index=8 + i, bid=4000.0 + i - 0.12, ask=4000.0 + i + 0.12) for i in range(20)]
    result = run_exit_only(ticks, ten_longs(), 15.0, policy="p", decision_every=1,
                           costs=CostModel(reject_first_n=3))
    assert result.close_failures > 0, "fixture: some closes were supposed to be rejected"
    assert result.positions_left_open == 0, "the intent stopped chasing after the price recovered"


def test_replay_reports_costs_and_labels_simulated_fills():
    ticks = descending_ticks()
    result = run_exit_only(ticks, ten_longs(), 15.0, policy="p", decision_every=1)
    assert result.commission_paid > 0
    assert result.net_result < result.gross_result, "commission must reduce the net result"
    assert any("simulated" in n for n in result.notes)
