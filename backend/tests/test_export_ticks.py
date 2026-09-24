"""The tick exporter, verified offline against a FAKE MetaTrader5 module.

No terminal is contacted here. A stub is installed in `sys.modules` before the
tool imports it, which is possible precisely because the tool imports MT5 inside
`main()` rather than at module scope.

Two things are being checked:

  * the tool cannot trade, and importing it does nothing
  * the coverage claims it makes are true — inclusive dates, chunking, resume,
    non-finite quotes, same-millisecond ticks, closures versus real gaps
"""

import csv
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

from tools import export_ticks

UTC = timezone.utc


class FakeTick:
    def __init__(self, stamp: datetime, bid: float, ask: float, flags: int = 6):
        self.time = int(stamp.timestamp())
        self.time_msc = int(stamp.timestamp() * 1000)
        self.bid, self.ask = bid, ask
        self.last, self.volume, self.flags = 0.0, 0, flags


class FakeMT5:
    """Records every call, so a test can assert what was and was not asked."""

    COPY_TICKS_INFO = 1

    def __init__(self, ticks_by_day=None, fail_days=(), none_days=()):
        self.ticks_by_day = ticks_by_day or {}
        self.fail_days = set(fail_days)
        self.none_days = set(none_days)
        self.calls: list[str] = []
        self.ranges: list[tuple] = []
        self.shutdown_called = False

    # -- the five calls the tool is allowed to make ------------------------
    def initialize(self):
        self.calls.append("initialize")
        return True

    def symbol_select(self, symbol, enable):
        self.calls.append(f"symbol_select:{symbol}")
        return True

    def copy_ticks_range(self, symbol, start, end, flags):
        self.calls.append("copy_ticks_range")
        self.ranges.append((start, end))
        day = start.date()
        if day in self.fail_days:
            raise RuntimeError("terminal dropped")
        if day in self.none_days:
            return None
        out = []
        cursor = start
        while cursor < end:
            out.extend(self.ticks_by_day.get(cursor.date(), []))
            cursor += timedelta(days=1)
        return out

    def version(self):
        return (500, 4000, "1 Jan 2026")

    def last_error(self):
        return (0, "no error")

    def shutdown(self):
        self.shutdown_called = True


@pytest.fixture
def fake_mt5(monkeypatch):
    def install(**kwargs):
        stub = FakeMT5(**kwargs)
        monkeypatch.setitem(sys.modules, "MetaTrader5", stub)
        return stub
    return install


def ticks_for(day, count=3, bid=4000.0, spread=0.24):
    base = datetime(day.year, day.month, day.day, 9, 0, tzinfo=UTC)
    return [FakeTick(base + timedelta(milliseconds=i * 500), bid + i * 0.01,
                     bid + i * 0.01 + spread) for i in range(count)]


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


# --- it cannot trade, and importing it does nothing --------------------------

def test_the_module_imports_without_touching_a_terminal():
    """Already imported at the top of this file. If importing it called
    initialize(), every test run would need a terminal."""
    assert "MetaTrader5" not in dir(export_ticks)


def test_the_source_contains_no_order_path_and_no_engine_import():
    from pathlib import Path

    source = Path(export_ticks.__file__).read_text()
    for forbidden in ("order_send", "order_check", "positions_get", "from app",
                      "import app", "bot_manager", "GridEngine"):
        assert forbidden not in source, f"the exporter references {forbidden}"


def test_only_read_only_mt5_calls_are_made(fake_mt5, tmp_path):
    day = datetime(2026, 6, 25, tzinfo=UTC).date()
    stub = fake_mt5(ticks_by_day={day: ticks_for(day)})
    code = export_ticks.main(["--symbol", "XAUUSDm", "--from", "2026-06-25",
                             "--to", "2026-06-25", "--out", str(tmp_path / "t.csv")])
    assert code == 0
    assert set(c.split(":")[0] for c in stub.calls) <= {
        "initialize", "symbol_select", "copy_ticks_range"}
    assert stub.shutdown_called, "the terminal connection was not closed"


# --- coverage claims ---------------------------------------------------------

def test_the_end_date_is_inclusive(fake_mt5, tmp_path):
    """The old tool took --to as a midnight boundary, so the last day was empty."""
    days = [datetime(2026, 6, 25, tzinfo=UTC).date(), datetime(2026, 6, 26, tzinfo=UTC).date()]
    stub = fake_mt5(ticks_by_day={d: ticks_for(d) for d in days})
    out = tmp_path / "t.csv"
    assert export_ticks.main(["--symbol", "XAUUSDm", "--from", "2026-06-25",
                              "--to", "2026-06-26", "--out", str(out)]) == 0

    rows = read_csv(out)
    exported_days = {r["time_utc"][:10] for r in rows}
    assert exported_days == {"2026-06-25", "2026-06-26"}
    assert stub.ranges[-1][1] == datetime(2026, 6, 27, tzinfo=UTC), (
        "the request must run to the start of the day AFTER the inclusive end"
    )


def test_each_day_is_requested_separately_by_default(fake_mt5, tmp_path):
    days = [datetime(2026, 6, 25, tzinfo=UTC).date() + timedelta(days=i) for i in range(3)]
    stub = fake_mt5(ticks_by_day={d: ticks_for(d) for d in days})
    export_ticks.main(["--symbol", "XAUUSDm", "--from", "2026-06-25", "--to", "2026-06-27",
                       "--out", str(tmp_path / "t.csv")])
    assert stub.calls.count("copy_ticks_range") == 3, (
        "three days must be three bounded requests, not one unbounded one"
    )


def test_ticks_sharing_a_millisecond_are_both_kept(fake_mt5, tmp_path):
    day = datetime(2026, 6, 25, tzinfo=UTC).date()
    same = datetime(2026, 6, 25, 9, 0, 0, tzinfo=UTC)
    stub = fake_mt5(ticks_by_day={day: [FakeTick(same, 4000.0, 4000.24),
                                        FakeTick(same, 4000.1, 4000.34)]})
    out = tmp_path / "t.csv"
    export_ticks.main(["--symbol", "XAUUSDm", "--from", "2026-06-25", "--to", "2026-06-25",
                       "--out", str(out)])
    rows = read_csv(out)
    assert len(rows) == 2, "a distinct observation was lost to a shared timestamp"
    assert rows[0]["time_utc"] == rows[1]["time_utc"]
    assert rows[0]["seq"] != rows[1]["seq"], "nothing distinguishes the two rows"


def test_non_finite_and_one_sided_quotes_are_skipped_and_counted(fake_mt5, tmp_path):
    day = datetime(2026, 6, 25, tzinfo=UTC).date()
    base = datetime(2026, 6, 25, 9, 0, tzinfo=UTC)
    stub = fake_mt5(ticks_by_day={day: [
        FakeTick(base, 4000.0, 4000.24),                        # good
        FakeTick(base + timedelta(seconds=1), 0.0, 4000.24),    # no bid
        FakeTick(base + timedelta(seconds=2), float("nan"), 4000.24),   # NaN slips past <= 0
        FakeTick(base + timedelta(seconds=3), 4000.0, float("inf")),    # inf too
    ]})
    out = tmp_path / "t.csv"
    export_ticks.main(["--symbol", "XAUUSDm", "--from", "2026-06-25", "--to", "2026-06-25",
                       "--out", str(out)])
    assert len(read_csv(out)) == 1
    manifest = json.loads((tmp_path / "t.csv.manifest.json").read_text())
    assert manifest["rows_skipped_no_finite_two_sided_quote"] == 3


def test_a_weekend_with_no_ticks_is_not_reported_as_a_gap(fake_mt5, tmp_path):
    # 2026-06-27 is a Saturday, 2026-06-28 a Sunday.
    friday = datetime(2026, 6, 26, tzinfo=UTC).date()
    stub = fake_mt5(ticks_by_day={friday: ticks_for(friday)})
    out = tmp_path / "t.csv"
    export_ticks.main(["--symbol", "XAUUSDm", "--from", "2026-06-26", "--to", "2026-06-28",
                       "--out", str(out)])
    manifest = json.loads((tmp_path / "t.csv.manifest.json").read_text())
    assert manifest["unexplained_gaps"] == []
    assert manifest["days_with_no_rows_expected_closed"] == 2


def test_an_empty_weekday_is_reported_as_an_unexplained_gap(fake_mt5, tmp_path):
    # 2026-06-25 Thursday with data, 2026-06-26 Friday empty.
    thursday = datetime(2026, 6, 25, tzinfo=UTC).date()
    stub = fake_mt5(ticks_by_day={thursday: ticks_for(thursday)})
    out = tmp_path / "t.csv"
    export_ticks.main(["--symbol", "XAUUSDm", "--from", "2026-06-25", "--to", "2026-06-26",
                       "--out", str(out)])
    manifest = json.loads((tmp_path / "t.csv.manifest.json").read_text())
    assert len(manifest["unexplained_gaps"]) == 1
    assert "2026-06-26" in manifest["unexplained_gaps"][0]
    assert manifest["coverage_is_complete"] is False


def test_a_dropped_terminal_is_recorded_rather_than_hidden(fake_mt5, tmp_path):
    days = [datetime(2026, 6, 25, tzinfo=UTC).date() + timedelta(days=i) for i in range(2)]
    stub = fake_mt5(ticks_by_day={d: ticks_for(d) for d in days}, fail_days=[days[1]])
    out = tmp_path / "t.csv"
    code = export_ticks.main(["--symbol", "XAUUSDm", "--from", "2026-06-25",
                              "--to", "2026-06-26", "--out", str(out)])
    manifest = json.loads((tmp_path / "t.csv.manifest.json").read_text())
    assert code == 0, "a partial export still writes what it got"
    assert any("terminal dropped" in e for e in manifest["errors"])
    assert manifest["coverage_is_complete"] is False
    assert len(read_csv(out)) == 3, "the day that worked must still be in the file"


def test_an_existing_file_is_not_overwritten_by_accident(fake_mt5, tmp_path):
    out = tmp_path / "t.csv"
    out.write_text("time_utc,bid,ask,last,volume,flags,seq\n")
    fake_mt5()
    code = export_ticks.main(["--symbol", "XAUUSDm", "--from", "2026-06-25",
                             "--to", "2026-06-25", "--out", str(out)])
    assert code == 2
    assert out.read_text().count("\n") == 1, "the existing export was modified"


def test_resume_continues_after_the_last_exported_day(fake_mt5, tmp_path):
    days = [datetime(2026, 6, 25, tzinfo=UTC).date() + timedelta(days=i) for i in range(2)]
    stub = fake_mt5(ticks_by_day={d: ticks_for(d) for d in days})
    out = tmp_path / "t.csv"
    export_ticks.main(["--symbol", "XAUUSDm", "--from", "2026-06-25", "--to", "2026-06-25",
                       "--out", str(out)])
    first_rows = read_csv(out)

    stub.ranges.clear()
    code = export_ticks.main(["--symbol", "XAUUSDm", "--from", "2026-06-25",
                              "--to", "2026-06-26", "--out", str(out), "--resume"])
    assert code == 0
    assert all(r[0].date() == days[1] for r in stub.ranges), (
        "resume re-requested a day that was already exported"
    )
    rows = read_csv(out)
    assert len(rows) == len(first_rows) * 2
    assert [int(r["seq"]) for r in rows] == list(range(1, len(rows) + 1)), (
        "the sequence must stay monotonic across a resume"
    )


def test_out_of_order_rows_are_counted(fake_mt5, tmp_path):
    day = datetime(2026, 6, 25, tzinfo=UTC).date()
    base = datetime(2026, 6, 25, 9, 0, tzinfo=UTC)
    stub = fake_mt5(ticks_by_day={day: [
        FakeTick(base + timedelta(seconds=5), 4000.0, 4000.24),
        FakeTick(base, 4000.0, 4000.24),          # earlier than the previous row
    ]})
    out = tmp_path / "t.csv"
    export_ticks.main(["--symbol", "XAUUSDm", "--from", "2026-06-25", "--to", "2026-06-25",
                       "--out", str(out)])
    manifest = json.loads((tmp_path / "t.csv.manifest.json").read_text())
    assert manifest["rows_out_of_order"] == 1
    assert manifest["coverage_is_complete"] is False


def test_the_manifest_carries_no_account_identifier(fake_mt5, tmp_path):
    day = datetime(2026, 6, 25, tzinfo=UTC).date()
    fake_mt5(ticks_by_day={day: ticks_for(day)})
    out = tmp_path / "t.csv"
    export_ticks.main(["--symbol", "XAUUSDm", "--from", "2026-06-25", "--to", "2026-06-25",
                       "--out", str(out)])
    text = (tmp_path / "t.csv.manifest.json").read_text().lower()
    for private in ("login", "password", "account", "server"):
        assert private not in text, f"the manifest leaked {private}"


def test_a_reversed_range_is_refused(fake_mt5, tmp_path):
    fake_mt5()
    assert export_ticks.main(["--symbol", "XAUUSDm", "--from", "2026-06-26",
                              "--to", "2026-06-25", "--out", str(tmp_path / "t.csv")]) == 2
