"""Scheduling: what slow reporting can and cannot do to the protective cadence.

The finding: `_loop` awaits `_maybe_report`, which runs `_reporting_tick`
synchronously on the same loop. A deterministic probe with a 10 ms configured
protective cadence and 250 ms of reporting work measured successive protective
STARTS 250 ms apart — the configured cadence was not what the loop delivered, and
a 25.9 ms benchmark of the protective FUNCTION said nothing about it.

What is fixed: reporting is staged against a time budget and defers the rest of
its work at the next stage boundary, and a cycle that still overruns puts
reporting into an absolute cooldown proportional to the time it consumed.

What is NOT fixed, and is asserted here so nobody claims otherwise: a synchronous
broker call already in flight cannot be interrupted. One cycle still absorbs it.
These are fake-clock scheduling probes. No MT5 latency is measured anywhere.
"""

import asyncio
import json
import time

import pytest

from app.engine.broker_owner import REPORTING
from tests.conftest import MAGIC


def live(**kw):
    kw.setdefault("basket_stop_loss_usd", 60.0)
    kw.setdefault("max_daily_loss_usd", 100.0)
    kw.setdefault("basket_take_profit_usd", 10_000.0)
    kw.setdefault("exit_commission_per_lot", 0.0)
    kw.setdefault("slippage_points_per_fill", 0.0)
    return kw


def run_loop(engine, *, cycles: int, reporting_ms: float):
    """Drives the real `_loop` and returns the gaps between protective starts."""
    starts: list[float] = []
    real_protective = engine._protective_tick

    def timed_protective():
        starts.append(time.monotonic())
        if len(starts) >= cycles:
            engine._running = False
        return real_protective()

    engine._protective_tick = timed_protective
    engine._reporting_tick = lambda *a, **k: time.sleep(reporting_ms / 1000.0)
    engine._running = True
    asyncio.run(engine._loop())
    return [(b - a) * 1000.0 for a, b in zip(starts, starts[1:])]


def test_fast_reporting_delivers_the_configured_cadence(broker, engine_factory):
    e = engine_factory(**live(), protective_poll_seconds=0.01, reporting_poll_seconds=0.0)
    gaps = run_loop(e, cycles=8, reporting_ms=0.0)
    assert max(gaps) < 60.0, f"a 10 ms cadence delivered {max(gaps):.0f} ms gaps"


def test_slow_reporting_no_longer_sets_the_cadence(broker, engine_factory):
    """The reviewer's probe, re-run against the fix.

    Before: every gap was ~250 ms. After: the cycle holding the in-flight call
    still absorbs it, and the rest run at the configured interval.
    """
    e = engine_factory(**live(), protective_poll_seconds=0.01, reporting_poll_seconds=0.0,
                       reporting_time_budget_ms=40.0)
    gaps = run_loop(e, cycles=14, reporting_ms=250.0)

    slow = [g for g in gaps if g > 100.0]
    assert len(slow) <= 2, (
        f"reporting still paced the loop: {len(slow)} of {len(gaps)} gaps exceeded 100 ms"
    )
    ordered = sorted(gaps)
    median = ordered[len(ordered) // 2]
    assert median < 60.0, f"median gap {median:.0f} ms — the cadence did not recover"
    assert e._reporting_overruns >= 1, "the overrun was not recorded"


def test_an_overrun_puts_reporting_into_a_cooldown(broker, engine_factory):
    e = engine_factory(**live(), protective_poll_seconds=0.01, reporting_poll_seconds=0.0,
                       reporting_time_budget_ms=20.0)
    run_loop(e, cycles=6, reporting_ms=200.0)
    assert e._reporting_cooldown_until_ms > 0.0, "no cooldown was set"
    assert e._reporting_overruns >= 1


def test_a_zero_interval_cannot_defeat_the_cooldown(broker, engine_factory):
    """The first attempt at this multiplied the configured interval, and an
    interval of zero stays zero however often it is doubled."""
    e = engine_factory(**live(), protective_poll_seconds=0.01, reporting_poll_seconds=0.0,
                       reporting_time_budget_ms=20.0)
    gaps = run_loop(e, cycles=12, reporting_ms=200.0)
    fast = [g for g in gaps if g < 60.0]
    assert len(fast) >= len(gaps) // 2, (
        "with reporting_poll_seconds=0 the cooldown did not hold reporting back"
    )


# --- staging: deferred, not dropped -----------------------------------------

def test_the_history_sweep_is_the_first_thing_deferred(broker, engine_factory, monkeypatch):
    e = engine_factory(**live())
    swept = []
    monkeypatch.setattr(e, "_sync_broker_history", lambda: swept.append(1))

    e._protective_tick()
    e._reporting_tick(deadline_ms=-1.0)          # budget already spent
    assert swept == [], "the history sweep ran with no time left"
    assert e._reporting_deferrals >= 1, "the deferral was not counted"

    e._reporting_tick(deadline_ms=None)          # no budget: everything runs
    assert swept == [1]


def test_the_entry_decision_is_deferred_rather_than_rushed(broker, engine_factory, monkeypatch):
    """A new grid can wait a cycle. A protective read cannot."""
    e = engine_factory(**live())
    considered = []
    monkeypatch.setattr(e, "_consider_entry",
                        lambda *a, **k: considered.append(1))

    e._protective_tick()
    broker.next_candle()
    # Spend the budget only AFTER the history sweep, so the entry stage is the
    # one that runs out of time.
    real_sync = e._sync_broker_history
    monkeypatch.setattr(e, "_sync_broker_history",
                        lambda: (real_sync(), time.sleep(0.03))[0])
    e._reporting_tick(deadline_ms=time.monotonic() * 1000.0 + 10.0)
    assert considered == [], "the entry decision ran with no time left"
    assert e._reporting_deferrals >= 1


def test_a_deferred_cycle_still_broadcasts_state(broker, engine_factory):
    """Deferring work must not leave the dashboard blind."""
    frames = []
    e = engine_factory(**live(), on_update=lambda snapshot: frames.append(snapshot))
    e._protective_tick()
    e._reporting_tick(deadline_ms=-1.0)
    assert frames, "a deferred reporting cycle sent nothing to the UI"
    # The note reaches the UI as `signal_reason`, which is the field the panel
    # renders; there is no top-level `note` in the snapshot contract.
    blob = json.dumps(frames[-1], default=str)
    assert "deferred" in blob, "the deferral was not explained to the owner"


# --- reporting reads go through the single owner -----------------------------

def test_reporting_reads_are_made_at_reporting_priority(broker, engine_factory):
    e = engine_factory(**live())
    seen = []
    real_call = e._owner.call

    def watched(name, fn, *a, priority="REPORTING", **k):
        seen.append((name, priority))
        return real_call(name, fn, *a, priority=priority, **k)

    e._owner.call = watched
    e._protective_tick()
    seen.clear()
    e._reporting_tick()

    reporting_reads = [name for name, priority in seen if priority == REPORTING]
    assert "positions" in reporting_reads, (
        "the reporting path still reads positions straight from the adapter, so the "
        "owner cannot yield to protective work between calls"
    )


def test_a_stalled_broker_makes_reporting_stand_aside(broker, engine_factory, monkeypatch):
    """SkipReporting is the correct outcome, not an error to swallow."""
    from app.engine.broker_owner import SkipReporting

    e = engine_factory(**live())
    e._protective_tick()
    monkeypatch.setattr(e._owner, "positions",
                        lambda *a, **k: (_ for _ in ()).throw(SkipReporting("protective work")))

    with pytest.raises(SkipReporting):
        e._reporting_tick()


# --- the measurements are separate, and labelled ----------------------------

def test_the_separate_latencies_are_reported_separately(broker, engine_factory):
    e = engine_factory(**live(), protective_poll_seconds=0.01, reporting_poll_seconds=0.0)
    run_loop(e, cycles=5, reporting_ms=0.0)
    scheduling = e.status()["scheduling"]

    for field in ("cadence_delay_ms", "protective_decision_ms", "reporting_cycle_ms",
                  "close_to_confirmed_flat_ms"):
        assert field in scheduling, f"{field} is not reported"
    assert scheduling["cadence_delay_ms"]["count"] >= 1, "the real cadence is not measured"
    assert "NOT measured" in scheduling["note"], (
        "the payload does not say that broker fill times are absent"
    )


def test_time_to_confirmed_flat_is_measured_from_the_decision(broker, engine_factory):
    e = engine_factory(**live())
    e._tick()
    broker.next_candle()
    e._tick()
    broker.price += 4.0
    broker.next_candle()
    e._tick()
    held = broker.get_open_positions("XAUUSD", magic=MAGIC)
    assert held, "fixture: something must be open"

    e._open_close_intent("owner_request", "measured close")
    e._drive_close_intent(held, [])
    stats = e.status()["scheduling"]["close_to_confirmed_flat_ms"]
    assert stats["count"] == 1, "the close-to-flat span was not recorded"


def test_a_restart_does_not_invent_a_close_duration(broker, engine_factory):
    """The monotonic start is gone after a restart; no span is fabricated."""
    e = engine_factory(**live())
    e._tick()
    broker.next_candle()
    e._tick()
    broker.price += 4.0
    broker.next_candle()
    e._tick()
    e._open_close_intent("owner_request", "close across a restart")

    fresh = engine_factory(**live())
    assert fresh._intent_started_ms is None
    held = broker.get_open_positions("XAUUSD", magic=MAGIC)
    fresh._drive_close_intent(held, broker.get_pending_orders("XAUUSD", magic=MAGIC))
    assert fresh.status()["scheduling"]["close_to_confirmed_flat_ms"]["count"] == 0, (
        "a duration was invented across a process boundary"
    )
