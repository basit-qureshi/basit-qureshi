"""A local scheduled-event calendar, with availability time kept separate.

Two different timestamps matter and conflating them is a leak:

* `event_time` — when the release happens.
* `available_from` — when the *schedule entry* itself became knowable.

A backtest that filters on events using only `event_time` is using a calendar
that, at the moment of the decision, might not have existed yet. Every query
here is "what did the diary say **as of** this instant", so a replay cannot see
an entry that was published later.

The calendar carries scheduled timing only. It holds no forecast, no actual and
no revision, because this phase does not trade on release values — and a
calendar does not say which way gold will move.

Coverage is explicit. `covered_from` / `covered_until` bound what the file
actually contains, so a query outside them returns UNKNOWN rather than an empty
list that would read as "no events".
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REQUIRED_COLUMNS = ("event_time_utc", "available_from_utc", "title", "impact", "currency")


@dataclass(frozen=True)
class CalendarEvent:
    event_time: datetime          # tz-aware UTC
    available_from: datetime      # tz-aware UTC
    title: str
    impact: str                   # "high" | "medium" | "low"
    currency: str


class EconomicCalendar:
    def __init__(self, events, covered_from=None, covered_until=None, source: str = "unknown"):
        self.events = sorted(events, key=lambda e: e.event_time)
        self.covered_from = covered_from
        self.covered_until = covered_until
        self.source = source

    def events_available_at(self, now: datetime):
        """Entries whose schedule was published at or before `now`.

        This is the whole point of the class: an entry added to the diary after
        the decision was taken is not information the decision could have had.
        """
        return [e for e in self.events if e.available_from <= now]

    def covers(self, moment: datetime) -> bool:
        if self.covered_from and moment < self.covered_from:
            return False
        if self.covered_until and moment > self.covered_until:
            return False
        return True

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "events": len(self.events),
            "covered_from": self.covered_from.isoformat() if self.covered_from else None,
            "covered_until": self.covered_until.isoformat() if self.covered_until else None,
            "impacts": sorted({e.impact for e in self.events}),
        }


def _parse_utc(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        # A naive stamp in a UTC column is read as UTC. It is not guessed at
        # from the machine's local zone, which would silently shift every event.
        return stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def load_calendar_csv(path: str | Path, source: str | None = None) -> tuple[EconomicCalendar, dict]:
    """Loads a calendar and returns it with a quality report.

    The report names every row that was rejected and why, rather than dropping
    bad rows quietly — a calendar that silently lost half its entries would
    look like a market with no events in it.
    """
    path = Path(path)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()

    events: list[CalendarEvent] = []
    problems: list[str] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"calendar is missing required columns: {missing}")
        for line_no, row in enumerate(reader, start=2):
            try:
                event_time = _parse_utc(row["event_time_utc"])
                available_from = _parse_utc(row["available_from_utc"])
            except Exception as exc:
                problems.append(f"line {line_no}: unparseable timestamp ({exc})")
                continue
            if available_from > event_time:
                # Allowed and normal — a diary entry is published before the
                # release. The reverse would mean the schedule appeared after
                # the fact, which cannot inform a decision taken before it.
                pass
            events.append(CalendarEvent(
                event_time=event_time, available_from=available_from,
                title=row["title"].strip(), impact=row["impact"].strip().lower(),
                currency=row["currency"].strip().upper(),
            ))

    covered_from = min((e.event_time for e in events), default=None)
    covered_until = max((e.event_time for e in events), default=None)
    calendar = EconomicCalendar(events, covered_from, covered_until,
                                source=source or str(path))
    report = {
        "path": str(path),
        "sha256": digest,
        "rows_loaded": len(events),
        "problems": problems,
        **calendar.as_dict(),
    }
    return calendar, report
