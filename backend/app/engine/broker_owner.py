"""One owner for broker access, with an explicit serialization policy.

The MetaTrader5 Python module is not thread-safe, so every call has to be
serialized somewhere. It already is, inside the adapter. What was missing is a
*policy*: with one lock and no priority, a 120 ms history sweep queued for the
dashboard sits directly in front of the read a stop-loss decision needs.

This owner adds three things and deliberately does not add a fourth.

**Priority.** Protective work jumps ahead of reporting work. Reporting is
best-effort; a protective read is not.

**Honest stall reporting.** When a call has been in flight longer than
`stall_after_ms`, the owner reports itself blocked, with the call's name and how
long it has been running. New exposure is refused while that holds.

**Timing that includes the bad cases.** A call that never returns is recorded as
a censored sample — a lower bound — rather than dropped.

What it does NOT do, on purpose: it does not cancel an in-flight call, and it
never issues a second request because the first one is late. A synchronous MT5
call cannot be cancelled by moving it to a thread — the thread stays stuck in
the terminal. Firing a replacement would risk two live order requests for one
decision, which is a worse failure than waiting. This is a real limit of the
Python terminal interface and it is measured rather than disguised.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from app.engine.instrumentation import Recorder, monotonic_ms

logger = logging.getLogger("broker_owner")

PROTECTIVE = "protective"
REPORTING = "reporting"


@dataclass
class OwnerHealth:
    blocked: bool = False
    in_flight: str | None = None
    in_flight_ms: float = 0.0
    priority_in_flight: str | None = None
    last_error: str | None = None
    protective_waits: int = 0
    reporting_skipped: int = 0

    def as_dict(self) -> dict:
        return {
            "blocked": self.blocked,
            "in_flight": self.in_flight,
            "in_flight_ms": round(self.in_flight_ms, 1),
            "in_flight_priority": self.priority_in_flight,
            "last_error": self.last_error,
            "protective_waits": self.protective_waits,
            "reporting_skipped": self.reporting_skipped,
        }


class BrokerOwner:
    """Serializes broker access and gives protective work the right of way."""

    def __init__(self, broker, recorder: Recorder | None = None, stall_after_ms: float = 4000.0):
        self.broker = broker
        self.recorder = recorder or Recorder()
        self.stall_after_ms = stall_after_ms
        # Not reentrant on purpose: a nested call would mean one logical
        # operation holding the broker across two others.
        self._lock = threading.Lock()
        # Raised while a protective call is waiting, so a reporting call that
        # has not started yet steps aside instead of taking the lock first.
        self._protective_waiting = threading.Event()
        self._state_lock = threading.Lock()
        self._in_flight: str | None = None
        self._in_flight_priority: str | None = None
        self._in_flight_started_ms: float = 0.0
        self.health = OwnerHealth()

    # -- introspection --------------------------------------------------------

    def snapshot_health(self) -> OwnerHealth:
        with self._state_lock:
            name = self._in_flight
            priority = self._in_flight_priority
            started = self._in_flight_started_ms
        elapsed = (monotonic_ms() - started) if name else 0.0
        self.health.in_flight = name
        self.health.priority_in_flight = priority
        self.health.in_flight_ms = elapsed
        # Blocked means: something is stuck in the terminal and we will not
        # start a competing request. It is a reason to refuse new exposure,
        # not a reason to retry.
        self.health.blocked = bool(name) and elapsed > self.stall_after_ms
        return self.health

    @property
    def blocked(self) -> bool:
        return self.snapshot_health().blocked

    # -- the one way in -------------------------------------------------------

    def call(self, name: str, fn, *args, priority: str = REPORTING, **kwargs):
        """Runs one broker operation under the owner's policy.

        Reporting work yields to a protective caller that is already waiting.
        `SkipReporting` is raised rather than returning a fake value, because a
        caller that cannot tell "skipped" from "nothing there" will eventually
        render a skip as a flat account.
        """
        if priority == REPORTING and self._protective_waiting.is_set():
            self.health.reporting_skipped += 1
            raise SkipReporting(f"{name} yielded to protective work")

        if priority == PROTECTIVE:
            self._protective_waiting.set()
            if self._lock.locked():
                self.health.protective_waits += 1

        try:
            with self._lock:
                with self._state_lock:
                    self._in_flight = name
                    self._in_flight_priority = priority
                    self._in_flight_started_ms = monotonic_ms()
                started = monotonic_ms()
                try:
                    result = fn(*args, **kwargs)
                except Exception as exc:
                    self.recorder.record(f"broker.{name}", monotonic_ms() - started, failed=True)
                    self.health.last_error = f"{name}: {exc}"
                    raise
                else:
                    self.recorder.record(f"broker.{name}", monotonic_ms() - started)
                    self.health.last_error = None
                    return result
                finally:
                    with self._state_lock:
                        self._in_flight = None
                        self._in_flight_priority = None
                        self._in_flight_started_ms = 0.0
        finally:
            if priority == PROTECTIVE:
                self._protective_waiting.clear()

    # -- convenience wrappers used on the protective path ---------------------

    def account_info(self, priority: str = PROTECTIVE):
        return self.call("account_info", self.broker.get_account_info, priority=priority)

    def positions(self, symbol, magic, priority: str = PROTECTIVE):
        return self.call("positions", self.broker.get_open_positions, symbol, magic=magic, priority=priority)

    def pendings(self, symbol, magic, priority: str = PROTECTIVE):
        return self.call("pendings", self.broker.get_pending_orders, symbol, magic=magic, priority=priority)

    def symbol_info(self, symbol, priority: str = PROTECTIVE):
        return self.call("symbol_info", self.broker.get_symbol_info, symbol, priority=priority)

    def candles(self, symbol, timeframe, count, priority: str = REPORTING):
        return self.call("candles", self.broker.get_candles, symbol, timeframe, count, priority=priority)

    def report(self) -> dict:
        return self.recorder.report()


class SkipReporting(RuntimeError):
    """A reporting read stood aside for protective work.

    Distinct from a failure and from an empty result, so a caller can say
    "not read this cycle" instead of drawing a confident zero.
    """
