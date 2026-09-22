"""Market information sources: an interface, and a local file adapter.

No network provider is shipped. That is a decision, not an omission: selecting
one requires checking its historical availability, timestamp quality, licence
terms and cost against a real account, and this task may not buy services or
consume paid APIs. Shipping a fabricated endpoint that "would work with a key"
is worse than shipping nothing, because it looks finished.

So what exists here is the part that can be finished honestly: the interface
every future provider must satisfy, and a local file adapter that reads what
the owner exports. Fixture tests run against the adapter, so the contract is
exercised rather than asserted.

The distinction this module is built around: **scheduled time, consensus,
actual release and revisions are four different fields with four different
availability times.** An actual figure cannot appear in a feature before it was
published AND ingested. A historical archive downloaded today proves nothing
about what the bot could have received a month ago, and `retrieved_at` is what
makes that visible instead of assumed.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class NewsItem:
    """One attributed item, with provenance and revision identity."""

    source_id: str
    item_id: str
    title: str
    url: str | None
    #: When the source published it.
    published_at: datetime
    #: When THIS system first had it. A backfill sets this to the backfill
    #: time, not to published_at, so a replay cannot pretend it was there.
    retrieved_at: datetime
    relevance: str = "unknown"
    #: Distinguishes an original from a later correction of the same story.
    revision: int = 0
    revises_item_id: str | None = None
    #: Only what the source's terms permit is stored. By default that is
    #: metadata and a headline, not full article text.
    body: str | None = None

    @property
    def available_at(self) -> datetime:
        """The earliest moment a decision could legitimately use this item.

        It is the LATER of publication and retrieval: an item published before
        the bot could fetch it was not available to the bot.
        """
        return max(self.published_at, self.retrieved_at)

    def as_dict(self) -> dict:
        return {
            "source_id": self.source_id, "item_id": self.item_id,
            "title": self.title, "url": self.url,
            "published_at": self.published_at.isoformat(),
            "retrieved_at": self.retrieved_at.isoformat(),
            "available_at": self.available_at.isoformat(),
            "relevance": self.relevance, "revision": self.revision,
            "revises_item_id": self.revises_item_id,
            "has_body": self.body is not None,
        }


@dataclass(frozen=True)
class ScheduledRelease:
    """A calendar entry with each field carrying its own availability.

    `actual` is the trap. It exists in a historical file from the moment the
    file is written, which is long after the release. `actual_available_at`
    is what stops it leaking into a feature computed for an earlier moment.
    """

    release_id: str
    title: str
    currency: str
    impact: str
    event_time: datetime
    schedule_available_at: datetime
    consensus: float | None = None
    consensus_available_at: datetime | None = None
    actual: float | None = None
    actual_available_at: datetime | None = None
    revision_of: str | None = None

    def visible_fields(self, now: datetime) -> dict:
        """Exactly the fields that existed at `now`. Nothing else."""
        fields = {}
        if self.schedule_available_at <= now:
            fields["event_time"] = self.event_time
            fields["impact"] = self.impact
        if self.consensus_available_at and self.consensus_available_at <= now:
            fields["consensus"] = self.consensus
        if self.actual_available_at and self.actual_available_at <= now:
            fields["actual"] = self.actual
        return fields


class ProviderUnavailable(RuntimeError):
    """A source could not be reached or is not configured.

    Raised rather than returning an empty list, because an empty list reads as
    "there was no news" and that is a different claim entirely.
    """


class NewsProvider:
    """The contract every source must satisfy."""

    source_id = "abstract"
    #: Documented so the cost of turning a provider on is known before it is.
    terms_note = "not specified"

    def fetch_items(self, since: datetime, until: datetime) -> list[NewsItem]:  # pragma: no cover
        raise NotImplementedError

    def fetch_schedule(self, since: datetime, until: datetime) -> list[ScheduledRelease]:  # pragma: no cover
        raise NotImplementedError

    def health(self) -> dict:  # pragma: no cover
        return {"source_id": self.source_id, "available": False,
                "reason": "abstract provider"}


@dataclass
class IngestionReport:
    """What came in, what was dropped, and what is simply not covered."""

    source_id: str
    items_read: int = 0
    items_kept: int = 0
    duplicates_collapsed: int = 0
    revisions_linked: int = 0
    rejected: list = field(default_factory=list)
    covered_from: datetime | None = None
    covered_until: datetime | None = None
    sha256: str | None = None

    def as_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "items_read": self.items_read, "items_kept": self.items_kept,
            "duplicates_collapsed": self.duplicates_collapsed,
            "revisions_linked": self.revisions_linked,
            "rejected": self.rejected[:20], "rejected_total": len(self.rejected),
            "covered_from": self.covered_from.isoformat() if self.covered_from else None,
            "covered_until": self.covered_until.isoformat() if self.covered_until else None,
            "sha256": self.sha256,
        }


def _utc(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)


class LocalFileNewsProvider(NewsProvider):
    """Reads an owner-exported CSV. Requires no credentials and no network.

    Required columns:
        source_id,item_id,title,url,published_at_utc,retrieved_at_utc
    Optional:
        relevance,revision,revises_item_id,body
    """

    source_id = "local_file"
    terms_note = "owner-supplied file; storage terms are the owner's to observe"
    REQUIRED = ("source_id", "item_id", "title", "published_at_utc", "retrieved_at_utc")

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._items: list[NewsItem] | None = None
        self.report = IngestionReport(source_id=self.source_id)

    def load(self) -> tuple[list[NewsItem], IngestionReport]:
        if not self.path.exists():
            raise ProviderUnavailable(
                f"{self.path} does not exist. No news source is configured, which is "
                f"UNKNOWN coverage — not an absence of news."
            )
        raw = self.path.read_bytes()
        self.report = IngestionReport(source_id=self.source_id,
                                      sha256=hashlib.sha256(raw).hexdigest())
        seen: dict[str, NewsItem] = {}
        with self.path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            missing = [c for c in self.REQUIRED if c not in (reader.fieldnames or [])]
            if missing:
                raise ValueError(f"news file is missing required columns: {missing}")
            for line_no, row in enumerate(reader, start=2):
                self.report.items_read += 1
                try:
                    item = NewsItem(
                        source_id=row["source_id"].strip(),
                        item_id=row["item_id"].strip(),
                        title=row["title"].strip(),
                        url=(row.get("url") or "").strip() or None,
                        published_at=_utc(row["published_at_utc"]),
                        retrieved_at=_utc(row["retrieved_at_utc"]),
                        relevance=(row.get("relevance") or "unknown").strip(),
                        revision=int(row.get("revision") or 0),
                        revises_item_id=(row.get("revises_item_id") or "").strip() or None,
                        body=(row.get("body") or None),
                    )
                except Exception as exc:
                    self.report.rejected.append(f"line {line_no}: {exc}")
                    continue

                existing = seen.get(item.item_id)
                if existing is None:
                    seen[item.item_id] = item
                elif item.revision > existing.revision:
                    # A later revision supersedes; both are counted so the
                    # collapse is visible rather than silent.
                    seen[item.item_id] = item
                    self.report.revisions_linked += 1
                else:
                    self.report.duplicates_collapsed += 1

        items = sorted(seen.values(), key=lambda i: i.available_at)
        self.report.items_kept = len(items)
        if items:
            self.report.covered_from = items[0].available_at
            self.report.covered_until = items[-1].available_at
        self._items = items
        return items, self.report

    def fetch_items(self, since: datetime, until: datetime) -> list[NewsItem]:
        if self._items is None:
            self.load()
        return [i for i in self._items if since <= i.available_at <= until]

    def items_available_at(self, now: datetime) -> list[NewsItem]:
        """Only what this system actually had by `now`."""
        if self._items is None:
            self.load()
        return [i for i in self._items if i.available_at <= now]

    def health(self) -> dict:
        return {
            "source_id": self.source_id,
            "available": self.path.exists(),
            "path": str(self.path),
            "terms": self.terms_note,
            "reason": None if self.path.exists() else "file not present",
            **({} if self._items is None else {"items": len(self._items)}),
        }
