"""Importing an MT5 tick export, and saying honestly what is in it.

MT5's `copy_ticks_range` returns bid, ask, last, volume and a UTC time. This
module reads a CSV export of that, keeps everything in UTC internally, and
produces a manifest with a content hash so a result can be tied to the exact
bytes it was computed from.

It also refuses to be quiet about gaps. A dataset with an unrecorded weekend,
a missing hour or a run of duplicate stamps will happily produce a confident
backtest, and the backtest will be wrong in a way nobody can see afterwards.
Every such issue is recorded in the quality report.

The export command for the owner to run on Windows is in
`REQUIRED_EXPORT`. Nothing in this repository connects to a terminal.
"""

from __future__ import annotations

import csv
import hashlib
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

REQUIRED_COLUMNS = ("time_utc", "bid", "ask")
OPTIONAL_COLUMNS = ("last", "volume", "flags")

REQUIRED_EXPORT = """\
# Run on the Windows machine with the MT5 terminal open and logged in.
# It reads history only; it places no orders.
#
#   .\\venv\\Scripts\\python.exe tools\\export_ticks.py --symbol XAUUSDm \\
#       --from 2026-06-01 --to 2026-09-20 --out data\\xauusdm_ticks.csv
#
# Required columns: time_utc,bid,ask       (optional: last,volume,flags)
# Required coverage for a usable Phase C evaluation:
#   * at least 3 months of continuous tick data for the traded symbol
#   * bid AND ask on every row (a mid-only export cannot price this strategy,
#     which pays the spread on up to 20 fills per basket)
#   * the SAME symbol the bot trades, including the broker's suffix
#   * UTC timestamps at tick resolution, not M1 bars
"""


@dataclass
class Tick:
    time: datetime      # tz-aware UTC
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass
class QualityReport:
    rows_read: int = 0
    rows_kept: int = 0
    rejected: list[str] = field(default_factory=list)
    duplicate_timestamps: int = 0
    out_of_order: int = 0
    non_positive_spread: int = 0
    gaps: list[dict] = field(default_factory=list)
    first: datetime | None = None
    last: datetime | None = None
    median_spread: float | None = None
    max_spread: float | None = None

    def as_dict(self) -> dict:
        return {
            "rows_read": self.rows_read,
            "rows_kept": self.rows_kept,
            "rejected": self.rejected[:50],
            "rejected_total": len(self.rejected),
            "duplicate_timestamps": self.duplicate_timestamps,
            "out_of_order": self.out_of_order,
            "non_positive_spread": self.non_positive_spread,
            "gaps": self.gaps[:50],
            "gap_count": len(self.gaps),
            "first_utc": self.first.isoformat() if self.first else None,
            "last_utc": self.last.isoformat() if self.last else None,
            "median_spread": self.median_spread,
            "max_spread": self.max_spread,
        }


def _parse_utc(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    stamp = datetime.fromisoformat(text)
    # A naive stamp in a column named *_utc is read as UTC. Reading it in the
    # machine's local zone would shift every tick by the operator's offset.
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)


def load_ticks_csv(path: str | Path, gap_threshold: timedelta = timedelta(minutes=5)):
    """Returns (ticks, report, sha256).

    Rows are kept in file order and checked, not silently sorted: a file that
    is out of order is a file with a problem, and quietly sorting it would hide
    that while changing what "the next tick" means.
    """
    path = Path(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    report = QualityReport()
    ticks: list[Tick] = []
    spreads: list[float] = []
    previous: datetime | None = None

    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(
                f"tick export is missing required columns {missing}. "
                f"This strategy pays the spread on every fill, so a mid-only "
                f"export cannot price it.\n\n{REQUIRED_EXPORT}"
            )
        for line_no, row in enumerate(reader, start=2):
            report.rows_read += 1
            try:
                stamp = _parse_utc(row["time_utc"])
                bid, ask = float(row["bid"]), float(row["ask"])
            except Exception as exc:
                report.rejected.append(f"line {line_no}: {exc}")
                continue
            if not (bid > 0 and ask > 0):
                report.rejected.append(f"line {line_no}: non-positive bid/ask")
                continue
            if ask < bid:
                report.non_positive_spread += 1
                report.rejected.append(f"line {line_no}: ask {ask} below bid {bid}")
                continue
            if previous is not None:
                if stamp < previous:
                    report.out_of_order += 1
                elif stamp == previous:
                    report.duplicate_timestamps += 1
                elif stamp - previous > gap_threshold:
                    report.gaps.append({
                        "from_utc": previous.isoformat(),
                        "to_utc": stamp.isoformat(),
                        "minutes": round((stamp - previous).total_seconds() / 60, 1),
                    })
            previous = stamp
            ticks.append(Tick(stamp, bid, ask))
            spreads.append(ask - bid)

    report.rows_kept = len(ticks)
    if ticks:
        report.first, report.last = ticks[0].time, ticks[-1].time
        report.median_spread = round(statistics.median(spreads), 5)
        report.max_spread = round(max(spreads), 5)
    return ticks, report, digest


def build_manifest(*, source_commit: str, config: dict, symbol_spec: dict,
                   dataset_path: str | None, dataset_sha256: str | None,
                   quality: QualityReport | None, costs: dict, fill_model: dict,
                   calendar_report: dict | None = None) -> dict:
    """Everything needed to reproduce a result, including what was absent.

    `dataset_path` of None is not an oversight — it records that an evaluation
    was attempted without market data, which is exactly the state a reader has
    to be able to see.
    """
    return {
        "manifest_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_commit": source_commit,
        "timezone": {
            "internal": "UTC",
            "display": "Asia/Karachi",
            "note": "All computation is UTC. PKT is a display concern only.",
        },
        "strategy_config": config,
        "symbol_spec": symbol_spec,
        "dataset": {
            "path": dataset_path,
            "sha256": dataset_sha256,
            "present": dataset_path is not None,
            "quality": quality.as_dict() if quality else None,
        },
        "calendar": calendar_report,
        "costs": costs,
        "fill_model": fill_model,
    }
