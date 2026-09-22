"""Session evidence: a manifest and an append-only event log.

What this is for. After a demo session somebody has to be able to answer
"what did the bot actually do", from a file, without trusting a dashboard
screenshot. That needs three things the existing instrumentation does not
provide: a manifest pinning what was running, a durable record of decisions
with their reasons, and a shareable copy with nothing private in it.

Four rules this module is built around.

**Capture must never delay protection.** Every public method here is
best-effort and swallows its own failures into a counter. A protective exit is
not allowed to wait on a disk write, and it is certainly not allowed to fail
because one did. `storage_errors` and `dropped_events` make that visible rather
than silent.

**Buffering is bounded.** A long session cannot grow the buffer without limit.
When the cap is reached the OLDEST in-memory event is dropped and counted; the
file, if one is open, already has it.

**History is never rewritten.** When settlement arrives later, or a figure is
corrected, a NEW event is appended that names the event it corrects. The
original stays exactly as it was recorded. A log you can edit is not evidence.

**A request is not a confirmation.** Submission, response and confirmed state
are three separate event kinds, so nobody can read "close sent" as "position
gone" - which is the specific confusion that made a failed close look like a
flat account in earlier phases.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

# --- event kinds -------------------------------------------------------------
# Deliberately explicit about the lifecycle stage. REQUEST_SENT and
# STATE_CONFIRMED are never the same event.
QUOTE_OBSERVED = "quote_observed"
ADMISSION_DECIDED = "admission_decided"
GRID_PLACED = "grid_placed"
REQUEST_SENT = "request_sent"
RESPONSE_RECEIVED = "response_received"
STATE_CONFIRMED = "state_confirmed"
CLOSE_INTENT_OPENED = "close_intent_opened"
CLOSE_INTENT_PROGRESS = "close_intent_progress"
CLOSE_INTENT_DONE = "close_intent_done"
SETTLEMENT = "settlement"
CORRECTION = "correction"
LIMIT_EVENT = "limit_event"
HALT = "halt"
PAUSE = "pause"
RESUME = "resume"
RECONNECT = "reconnect"
COVERAGE_GAP = "coverage_gap"
EXPOSURE_SNAPSHOT = "exposure_snapshot"
SHADOW_PREDICTION = "shadow_prediction"

#: Keys whose VALUES never reach the shareable packet.
REDACT_KEYS = {
    "mt5_login", "mt5_password", "mt5_server", "password", "token", "secret",
    "api_key", "login", "server", "database_url", "account_id",
    "observation_account_id", "broker_id",
}

#: Substrings that make any key sensitive regardless of its prefix, so
#: `broker_password` and `news_api_key` are caught without being listed.
REDACT_SUBSTRINGS = ("password", "passwd", "token", "secret", "api_key", "apikey")

#: What a removed value is replaced by. The export tool checks for exactly
#: this, so it lives here rather than being spelled out in two places.
REDACTED_MARKER = "<redacted>"

#: Keys replaced by a stable reference instead of the marker.
ACCOUNT_REF_KEYS = ("account_id", "observation_account_id")


def is_sensitive_key(key) -> bool:
    """True when this key's value must not appear in a shareable packet."""
    lowered = str(key).lower()
    return lowered in REDACT_KEYS or any(s in lowered for s in REDACT_SUBSTRINGS)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git_revision() -> str:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                                text=True, timeout=5,
                                cwd=str(Path(__file__).resolve().parents[3]))
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def stable_account_ref(account_id: str | None) -> str:
    """A stable, non-reversible handle for one account.

    The packet has to be reconcilable against local records without carrying
    the broker login around. A salted-by-nothing hash is fine here: the point
    is not secrecy against an attacker who already has the account number, it
    is that the number is not sitting in a file the owner emails to someone.
    """
    if not account_id:
        return "unknown"
    return "acct_" + hashlib.sha256(account_id.encode()).hexdigest()[:12]


def redact(value):
    """Recursively removes private values, keeping structure and key names."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if is_sensitive_key(key):
                if str(key).lower() in ACCOUNT_REF_KEYS:
                    out[key] = stable_account_ref(None if item is None else str(item))
                else:
                    out[key] = REDACTED_MARKER
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


@dataclass
class SessionManifest:
    """What was running. Written once, at the start."""

    session_id: str
    started_at_utc: str
    source_revision: str
    profile_key: str
    profile_frozen: bool
    strategy_config: dict
    symbol: str
    accounting_timezone: str
    display_timezone: str
    ai_mode: str
    #: "verified_demo", "verified_real", "unverified". Stays UNVERIFIED until
    #: the broker itself has been asked - a local ACCOUNT_TYPE setting is not
    #: an observation of anything.
    account_type: str = "unverified"
    account_ref: str = "unknown"
    starting_balance: float | None = None
    starting_equity: float | None = None
    dependency_versions: dict = field(default_factory=dict)
    coverage: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "started_at_utc": self.started_at_utc,
            "source_revision": self.source_revision,
            "profile_key": self.profile_key,
            "profile_frozen": self.profile_frozen,
            "strategy_config": self.strategy_config,
            "symbol": self.symbol,
            "accounting_timezone": self.accounting_timezone,
            "display_timezone": self.display_timezone,
            "ai_mode": self.ai_mode,
            "account_type": self.account_type,
            "account_ref": self.account_ref,
            "starting_balance": self.starting_balance,
            "starting_equity": self.starting_equity,
            "dependency_versions": self.dependency_versions,
            "coverage": self.coverage,
            "notes": self.notes,
        }


def build_manifest(*, profile_key: str, strategy_config: dict, symbol: str,
                   accounting_timezone: str, ai_mode: str,
                   account_info=None, account_verified: bool = False,
                   session_id: str | None = None) -> SessionManifest:
    """Assembles a manifest. Account type is UNVERIFIED unless it was observed.

    `account_verified` is the engine's own verification result, not the app's
    demo/real setting. Passing the setting here would defeat the entire point
    of the field.
    """
    account_type = "unverified"
    account_ref = "unknown"
    starting_balance = starting_equity = None
    if account_info is not None and account_verified:
        broker_mode = getattr(account_info, "trade_mode", "unknown")
        if broker_mode in ("demo", "real"):
            account_type = f"verified_{broker_mode}"
        account_ref = stable_account_ref(getattr(account_info, "account_id", None))
        starting_balance = getattr(account_info, "balance", None)
        starting_equity = getattr(account_info, "equity", None)
    elif account_info is not None:
        account_ref = stable_account_ref(getattr(account_info, "account_id", None))
        starting_balance = getattr(account_info, "balance", None)
        starting_equity = getattr(account_info, "equity", None)

    return SessionManifest(
        session_id=session_id or f"sess-{uuid.uuid4().hex[:12]}",
        started_at_utc=_utc_now(),
        source_revision=_git_revision(),
        profile_key=profile_key,
        profile_frozen=True,
        strategy_config=dict(strategy_config),
        symbol=symbol,
        accounting_timezone=accounting_timezone,
        display_timezone="Asia/Karachi",
        ai_mode=ai_mode,
        account_type=account_type,
        account_ref=account_ref,
        starting_balance=starting_balance,
        starting_equity=starting_equity,
        dependency_versions={"python": platform.python_version()},
        notes=[
            "Times in this file are UTC. Asia/Karachi is a display concern only.",
            "request_sent, response_received and state_confirmed are separate "
            "events: a sent request is not a confirmed outcome.",
        ],
    )


class SessionEvidence:
    """The recorder. Bounded, append-only, and unable to break the caller."""

    def __init__(self, manifest: SessionManifest, directory: str | Path | None = None,
                 capacity: int = 20_000):
        self.manifest = manifest
        self.capacity = capacity
        self.events: list[dict] = []
        self.dropped_events = 0
        self.storage_errors: list[str] = []
        self._sequence = 0
        self._path: Path | None = None
        if directory is not None:
            try:
                directory = Path(directory)
                directory.mkdir(parents=True, exist_ok=True)
                (directory / f"{manifest.session_id}.manifest.json").write_text(
                    json.dumps(manifest.as_dict(), indent=2), encoding="utf-8")
                self._path = directory / f"{manifest.session_id}.events.jsonl"
                self._path.touch()
            except Exception as exc:
                # A storage problem is recorded and the session continues in
                # memory. Losing evidence is bad; stopping the bot because a
                # disk is full would be worse.
                self.storage_errors.append(f"could not open evidence directory: {exc}")
                self._path = None

    # -- recording ---------------------------------------------------------

    def record(self, kind: str, **payload) -> dict | None:
        """Appends one event. Never raises, whatever the caller does.

        Returns the event so a caller can reference its id in a later
        correction; returns None only if recording itself failed.
        """
        try:
            self._sequence += 1
            event = {
                "seq": self._sequence,
                "event_id": f"{self.manifest.session_id}-{self._sequence:08d}",
                "at_utc": _utc_now(),
                "kind": kind,
                **payload,
            }
            self.events.append(event)
            if len(self.events) > self.capacity:
                # Oldest out. The file already has it if a file is open.
                del self.events[0]
                self.dropped_events += 1
            self._append_to_file(event)
            return event
        except Exception as exc:                      # pragma: no cover - defensive
            self.storage_errors.append(f"record({kind}) failed: {exc}")
            return None

    def _append_to_file(self, event: dict) -> None:
        if self._path is None:
            return
        try:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, default=str) + "\n")
        except Exception as exc:
            self.storage_errors.append(f"append failed: {exc}")
            # Stop trying after repeated failures rather than thrashing a bad
            # disk on every protective cycle.
            if len(self.storage_errors) > 20:
                self._path = None
                self.storage_errors.append("file writing disabled after repeated failures")

    def correct(self, corrects_event_id: str, reason: str, **payload) -> dict | None:
        """Appends a CORRECTION that links to the original.

        The original is never edited. A settlement arriving hours later adds a
        row pointing at the row it revises, so the sequence of what was known
        and when stays readable.
        """
        return self.record(CORRECTION, corrects_event_id=corrects_event_id,
                           reason=reason, **payload)

    def note_gap(self, reason: str, **payload) -> dict | None:
        """Explicit coverage loss. Absence of events is not absence of activity."""
        return self.record(COVERAGE_GAP, reason=reason, **payload)

    # -- reading -----------------------------------------------------------

    def of_kind(self, kind: str) -> list[dict]:
        return [e for e in self.events if e["kind"] == kind]

    def coverage(self) -> dict:
        return {
            "events_recorded": self._sequence,
            "events_in_memory": len(self.events),
            "dropped_events": self.dropped_events,
            "storage_errors": self.storage_errors[:20],
            "storage_error_count": len(self.storage_errors),
            "file": str(self._path) if self._path else None,
            "coverage_gaps": len(self.of_kind(COVERAGE_GAP)),
            "complete": self.dropped_events == 0 and not self.storage_errors,
        }

    # -- sharing -----------------------------------------------------------

    def shareable(self) -> dict:
        """The redacted packet. Structure preserved, private values removed."""
        manifest = redact(self.manifest.as_dict())
        return {
            "schema_version": SCHEMA_VERSION,
            "manifest": manifest,
            "coverage": self.coverage(),
            "events": [redact(e) for e in self.events],
            "disclaimer": (
                "Demo observations are not live-account evidence. Simulated and "
                "shadow entries are marked as such and are not executed fills."
            ),
        }

    def write_packet(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.shareable(), indent=2, default=str)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
        return path


class NullEvidence:
    """Used when no session is running. Every call is a cheap no-op.

    Having this means the engine can call `evidence.record(...)` unconditionally
    without an `if` at every site, and without a session being required for the
    bot to run.
    """

    manifest = None
    dropped_events = 0
    storage_errors: list = []

    def record(self, kind: str, **payload):
        return None

    def correct(self, *a, **k):
        return None

    def note_gap(self, *a, **k):
        return None

    def of_kind(self, kind: str) -> list:
        return []

    def coverage(self) -> dict:
        return {"events_recorded": 0, "active": False}

    def shareable(self) -> dict:
        return {"manifest": None, "events": [], "coverage": self.coverage()}
