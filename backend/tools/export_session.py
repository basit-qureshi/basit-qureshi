"""Build a redacted evidence packet from a recorded session. OFFLINE.

    .\\venv\\Scripts\\python.exe tools\\export_session.py --session sess-abc123
    .\\venv\\Scripts\\python.exe tools\\export_session.py --list

Reads the files the running backend wrote under `evidence\\`. It contacts no
broker, opens no network connection and reads no credentials.

The output is the copy that is safe to send for review: passwords, logins,
server names, tokens and local database paths are removed, and the account
identifier is replaced by a stable non-reversible reference so separate files
from the same account still reconcile.

It refuses to write a packet it cannot verify as redacted — a scan runs over
the finished file and the export aborts if anything that looks private
survived.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.evidence.session import (  # noqa: E402
    REDACTED_MARKER,
    SCHEMA_VERSION,
    is_sensitive_key,
    redact,
)

#: The only values a sensitive key is allowed to carry into a packet.
ALLOWED_VALUES = {REDACTED_MARKER, "unknown", ""}

#: A sensitive key and its value inside the SERIALIZED text, including the
#: escaped form that appears when a payload nested a JSON blob inside a string.
#: The value is captured and compared, never asserted with a lookahead: a
#: lookahead after `\s*` can be satisfied by backtracking the whitespace to
#: nothing, which makes the guard pass over text it was written to catch.
KEYED_VALUE = re.compile(
    r'\\?"([A-Za-z0-9_]*(?:password|passwd|token|secret|api_key|apikey|login|'
    r'database_url)[A-Za-z0-9_]*)\\?"\s*:\s*'
    r'(\\?"(?:[^"\\]|\\.)*?\\?"|-?\d+(?:\.\d+)?|null|true|false)',
    re.I,
)

#: Things that are never a key/value pair but still must not travel.
FORBIDDEN_TEXT = [
    (re.compile(r'[A-Za-z]:\\\\Users\\\\', re.I), "a local user path"),
    (re.compile(r'[A-Za-z]:\\Users\\', re.I), "a local user path"),
]


def _value_is_clean(value) -> bool:
    """A sensitive key may hold the marker, a stable reference, or nothing."""
    if value is None:
        return True
    if isinstance(value, bool) or isinstance(value, (int, float)):
        # A number under a login key is exactly the leak this guard exists for.
        return False
    if isinstance(value, str):
        return value in ALLOWED_VALUES or value.startswith("acct_")
    return False


def scan_structure(node, path: str = "") -> list[str]:
    """Walks the packet and reports sensitive keys that kept their values."""
    problems: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            if is_sensitive_key(key):
                if not _value_is_clean(value):
                    problems.append(f"an unredacted value at {here}")
            else:
                problems.extend(scan_structure(value, here))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            problems.extend(scan_structure(value, f"{path}[{index}]"))
    return problems


def scan_text(text: str) -> list[str]:
    """Backstop over the serialized form.

    The structural walk cannot see a secret that was embedded inside a string -
    an exception message that quoted a config blob, say. This reads the text
    the owner would actually send.
    """
    problems: list[str] = []
    for match in KEYED_VALUE.finditer(text):
        key, raw = match.group(1), match.group(2)
        if raw in ("null", "true", "false"):
            continue
        value = raw.replace('\\"', '"')
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = value[1:-1]
        if not _value_is_clean(value):
            problems.append(f'an unredacted "{key}" in the serialized text')
    for pattern, label in FORBIDDEN_TEXT:
        if pattern.search(text):
            problems.append(label)
    return problems


def scan(text: str, packet=None) -> list[str]:
    """Every reason this file is not safe to send, de-duplicated."""
    problems = scan_structure(packet) if packet is not None else []
    problems.extend(scan_text(text))
    seen, unique = set(), []
    for problem in problems:
        if problem not in seen:
            seen.add(problem)
            unique.append(problem)
    return unique


def locate(packet: dict) -> list[tuple[str, str, list[str]]]:
    """Which recorded events carry text the scan refuses to send."""
    found = []
    for event in packet.get("events", []):
        labels = scan(json.dumps(event, default=str), event)
        if labels:
            found.append((event.get("event_id", "?"), event.get("kind", "?"), labels))
    if scan(json.dumps(packet.get("manifest", {}), default=str), packet.get("manifest")):
        found.insert(0, ("manifest", "manifest", ["see the manifest"]))
    return found


def load_session(directory: Path, session_id: str) -> dict:
    manifest_path = directory / f"{session_id}.manifest.json"
    events_path = directory / f"{session_id}.events.jsonl"
    if not manifest_path.exists():
        raise FileNotFoundError(f"no manifest at {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    events, malformed = [], 0
    if events_path.exists():
        for line in events_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                # A torn final line from an interrupted write. Counted, not
                # silently dropped: a missing event is a coverage question.
                malformed += 1
    return {"manifest": manifest, "events": events, "malformed_lines": malformed}


def summarize(events: list[dict]) -> dict:
    """Counts by kind, plus the two questions a reviewer asks first."""
    kinds: dict[str, int] = {}
    for event in events:
        kinds[event.get("kind", "?")] = kinds.get(event.get("kind", "?"), 0) + 1
    closes_opened = kinds.get("close_intent_opened", 0)
    closes_confirmed = kinds.get("close_intent_done", 0)
    return {
        "events": len(events),
        "by_kind": dict(sorted(kinds.items())),
        "close_intents_opened": closes_opened,
        "close_intents_confirmed_flat": closes_confirmed,
        "close_intents_unconfirmed": max(0, closes_opened - closes_confirmed),
        "coverage_gaps": kinds.get("coverage_gap", 0),
        "corrections": kinds.get("correction", 0),
        "first_event_utc": events[0]["at_utc"] if events else None,
        "last_event_utc": events[-1]["at_utc"] if events else None,
    }


def main_with(argv: list[str] | None = None) -> int:
    """The whole tool, with argv passed in so the tests can drive it directly."""
    parser = argparse.ArgumentParser(description="Export a redacted evidence packet.")
    parser.add_argument("--dir", default="evidence", help="where sessions were written")
    parser.add_argument("--session", help="session id, e.g. sess-abc123def456")
    parser.add_argument("--list", action="store_true", help="list recorded sessions")
    parser.add_argument("--out", help="destination file (default: <session>.packet.json)")
    args = parser.parse_args(argv)

    directory = Path(args.dir)
    if not directory.exists():
        print(f"no evidence directory at {directory.resolve()}.", file=sys.stderr)
        print("No session has been recorded yet. Start one with "
              "POST /api/session/start while the backend is running.", file=sys.stderr)
        return 2

    sessions = sorted(p.name.replace(".manifest.json", "")
                      for p in directory.glob("*.manifest.json"))
    if args.list or not args.session:
        if not sessions:
            print(f"no sessions recorded under {directory.resolve()}")
            return 2
        print(f"sessions under {directory.resolve()}:")
        for name in sessions:
            data = load_session(directory, name)
            summary = summarize(data["events"])
            print(f"  {name}  events={summary['events']:<6} "
                  f"gaps={summary['coverage_gaps']}  "
                  f"started={data['manifest'].get('started_at_utc')}")
        if not args.session:
            print("\npass --session <id> to export one.")
        return 0

    try:
        data = load_session(directory, args.session)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3

    packet = {
        "schema_version": SCHEMA_VERSION,
        "exported_at_utc": datetime.now(timezone.utc).isoformat(),
        "manifest": redact(data["manifest"]),
        "summary": summarize(data["events"]),
        "malformed_lines": data["malformed_lines"],
        "events": [redact(e) for e in data["events"]],
        "disclaimer": (
            "Demo observations are not live-account evidence. Shadow predictions "
            "and replay outcomes are simulated and are not executed fills. "
            "Trading performance is not established by this packet."
        ),
    }

    text = json.dumps(packet, indent=2, default=str)
    problems = scan(text, packet)
    if problems:
        print("ERROR: the packet still contains " + ", ".join(problems) +
              " — refusing to write it. Nothing was saved.", file=sys.stderr)
        # Refusing is only half an answer; the owner needs to know which record
        # to look at. Key-shaped secrets get redacted by structure, so what
        # survives is almost always text somebody logged into a message.
        offenders = locate(packet)
        if offenders:
            print("\nThe text came from:", file=sys.stderr)
            for event_id, kind, labels in offenders[:10]:
                print(f"  {event_id}  ({kind}): {', '.join(labels)}", file=sys.stderr)
            if len(offenders) > 10:
                print(f"  ... and {len(offenders) - 10} more", file=sys.stderr)
        print("\nOpen those events in the .events.jsonl file and check what was "
              "logged. Do not edit the log to make this pass — the log is the "
              "evidence. Fix what writes that message.", file=sys.stderr)
        return 4

    out = Path(args.out or f"{args.session}.packet.json")
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(out)

    summary = packet["summary"]
    print(f"wrote {out}  ({summary['events']} events)")
    print(f"  close intents opened      : {summary['close_intents_opened']}")
    print(f"  confirmed flat            : {summary['close_intents_confirmed_flat']}")
    print(f"  UNCONFIRMED               : {summary['close_intents_unconfirmed']}")
    print(f"  coverage gaps             : {summary['coverage_gaps']}")
    print(f"  corrections               : {summary['corrections']}")
    if data["malformed_lines"]:
        print(f"  ! {data['malformed_lines']} malformed line(s) — coverage is incomplete")
    print("\nredaction scan passed. This file is safe to send for review.")
    return 0


def main() -> int:
    return main_with(None)


if __name__ == "__main__":
    raise SystemExit(main())
