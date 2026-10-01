"""Phase F: session evidence capture, verified with fixtures.

No demo session has occurred. These tests verify the COLLECTOR — that it
records the right things, redacts the right things, never rewrites history and
can never delay or break protection. They say nothing about trading results,
because there are none.
"""

import json
from datetime import datetime, timezone

import pytest

from app.evidence import session as ev
from tests.conftest import MAGIC
from tools import export_session

UTC = timezone.utc


def manifest(**over):
    base = dict(profile_key="baseline@v1",
                strategy_config={"grid_lot_size": 0.01, "grid_distance": 0.30},
                symbol="XAUUSD", accounting_timezone="Asia/Karachi",
                ai_mode="disabled")
    base.update(over)
    return ev.build_manifest(**base)


def orders(broker):
    return broker.get_pending_orders("XAUUSD", magic=MAGIC)


def live(**kw):
    kw.setdefault("basket_stop_loss_usd", 60.0)
    kw.setdefault("max_daily_loss_usd", 10_000.0)
    kw.setdefault("basket_take_profit_usd", 10_000.0)
    return kw


# --- manifest -----------------------------------------------------------------

def test_account_type_is_unverified_until_the_broker_was_actually_asked():
    """The app's demo/real setting is not an observation of anything."""
    m = manifest()
    assert m.account_type == "unverified"
    assert m.account_ref == "unknown"


def test_an_unverified_account_is_still_unverified_even_with_account_info(broker):
    """Reading the account is not the same as verifying it."""
    m = manifest(account_info=broker.get_account_info(), account_verified=False)
    assert m.account_type == "unverified"
    assert m.starting_balance is not None, "balance is still recorded"


def test_a_verified_demo_account_is_labelled_from_the_broker(broker):
    info = broker.get_account_info()
    assert info.trade_mode == "demo"
    m = manifest(account_info=info, account_verified=True)
    assert m.account_type == "verified_demo"
    assert m.account_ref.startswith("acct_")
    assert info.account_id not in m.account_ref, "the raw account id leaked into the ref"


def test_the_manifest_pins_what_was_running():
    m = manifest().as_dict()
    for key in ("source_revision", "profile_key", "profile_frozen", "strategy_config",
                "symbol", "accounting_timezone", "ai_mode", "started_at_utc"):
        assert key in m, f"the manifest does not pin {key}"
    assert m["profile_frozen"] is True
    assert m["display_timezone"] == "Asia/Karachi"


# --- the request / confirmation distinction ----------------------------------

def test_submission_and_confirmation_are_different_events():
    """The confusion that made a failed close look like a flat account."""
    rec = ev.SessionEvidence(manifest())
    rec.record(ev.REQUEST_SENT, basket_id="b1", what="close")
    rec.record(ev.CLOSE_INTENT_PROGRESS, basket_id="b1", confirmed_flat=False,
               positions_remaining=3)
    rec.record(ev.CLOSE_INTENT_DONE, basket_id="b1", confirmed_flat=True)

    kinds = [e["kind"] for e in rec.events]
    assert kinds == [ev.REQUEST_SENT, ev.CLOSE_INTENT_PROGRESS, ev.CLOSE_INTENT_DONE]
    progress = rec.of_kind(ev.CLOSE_INTENT_PROGRESS)[0]
    assert progress["confirmed_flat"] is False


# --- history is appended, never rewritten -------------------------------------

def test_a_later_settlement_links_a_correction_and_leaves_the_original(tmp_path):
    rec = ev.SessionEvidence(manifest(), directory=tmp_path)
    original = rec.record(ev.SETTLEMENT, ticket="t1", net=None,
                          note="closed at the broker, figure not yet reported")
    rec.correct(original["event_id"], reason="broker reported the realized figure",
                ticket="t1", net=-4.25)

    settlements = rec.of_kind(ev.SETTLEMENT)
    assert len(settlements) == 1 and settlements[0]["net"] is None, (
        "the original event was rewritten"
    )
    correction = rec.of_kind(ev.CORRECTION)[0]
    assert correction["corrects_event_id"] == original["event_id"]
    assert correction["net"] == -4.25


def test_events_are_append_only_on_disk(tmp_path):
    rec = ev.SessionEvidence(manifest(), directory=tmp_path)
    for i in range(5):
        rec.record(ev.QUOTE_OBSERVED, price=4000.0 + i)
    path = next(tmp_path.glob("*.events.jsonl"))
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["seq"] for r in rows] == [1, 2, 3, 4, 5]
    assert [r["price"] for r in rows] == [4000.0, 4001.0, 4002.0, 4003.0, 4004.0]


# --- bounded buffering and explicit coverage loss -----------------------------

def test_the_buffer_is_bounded_and_the_loss_is_counted():
    rec = ev.SessionEvidence(manifest(), capacity=10)
    for i in range(25):
        rec.record(ev.QUOTE_OBSERVED, price=float(i))
    assert len(rec.events) == 10
    assert rec.dropped_events == 15
    coverage = rec.coverage()
    assert coverage["events_recorded"] == 25, "the true count must survive trimming"
    assert coverage["complete"] is False


def test_a_coverage_gap_is_recorded_rather_than_left_as_silence():
    """Absence of events is not absence of activity."""
    rec = ev.SessionEvidence(manifest())
    rec.note_gap("broker unreadable for 40s", seconds=40)
    assert rec.coverage()["coverage_gaps"] == 1


def test_a_storage_failure_is_surfaced_and_does_not_raise(tmp_path, monkeypatch):
    rec = ev.SessionEvidence(manifest(), directory=tmp_path)

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(type(rec._path), "open", broken, raising=False)
    rec.record(ev.QUOTE_OBSERVED, price=4000.0)     # must not raise
    assert rec.coverage()["storage_error_count"] >= 1
    assert rec.coverage()["complete"] is False


def test_repeated_storage_failures_stop_thrashing_the_disk(tmp_path, monkeypatch):
    rec = ev.SessionEvidence(manifest(), directory=tmp_path)

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(type(rec._path), "open", broken, raising=False)
    for _ in range(30):
        rec.record(ev.QUOTE_OBSERVED, price=4000.0)
    assert rec._path is None, "the recorder kept retrying a failing disk every cycle"
    assert any("disabled" in e for e in rec.storage_errors)


def test_an_unusable_directory_does_not_stop_the_session(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file")
    rec = ev.SessionEvidence(manifest(), directory=blocker)
    assert rec._path is None
    assert rec.storage_errors, "the failure was not recorded"
    rec.record(ev.QUOTE_OBSERVED, price=4000.0)     # in-memory capture continues
    assert len(rec.events) == 1


# --- redaction ----------------------------------------------------------------

def test_the_shareable_packet_omits_private_values():
    rec = ev.SessionEvidence(manifest())
    rec.record("debug", mt5_password="hunter2", mt5_login=12345678,
               mt5_server="Exness-Real7", api_key="sk-abc",
               database_url="sqlite:///C:/Users/Home/trading_bot.db",
               price=4000.0)
    packet = rec.shareable()
    blob = json.dumps(packet)
    for secret in ("hunter2", "12345678", "Exness-Real7", "sk-abc", "Users/Home"):
        assert secret not in blob, f"{secret!r} reached the shareable packet"
    assert packet["events"][0]["price"] == 4000.0, "redaction destroyed useful data"


def test_the_account_reference_is_stable_but_not_reversible():
    first = ev.stable_account_ref("mt5:Exness-MT5Trial:123456")
    second = ev.stable_account_ref("mt5:Exness-MT5Trial:123456")
    other = ev.stable_account_ref("mt5:Exness-MT5Trial:999999")
    assert first == second, "the same account must reconcile across files"
    assert first != other
    assert "123456" not in first


def test_redaction_keeps_structure_and_key_names():
    out = ev.redact({"a": {"password": "x", "keep": 1}, "b": [{"token": "y", "n": 2}]})
    assert out == {"a": {"password": "<redacted>", "keep": 1},
                   "b": [{"token": "<redacted>", "n": 2}]}


def test_the_packet_carries_its_own_disclaimer():
    packet = ev.SessionEvidence(manifest()).shareable()
    assert "not live-account evidence" in packet["disclaimer"]


# --- capture must never delay or break protection ----------------------------

def test_a_recorder_that_fails_cannot_break_a_protective_exit(broker, engine_factory):
    """Evidence capture is optional. A recorder that throws on every call must
    not be able to stop a stop-loss, which is why the engine guards the call
    site rather than trusting the recorder to behave."""
    class Hostile(ev.NullEvidence):
        def record(self, kind, **payload):
            raise RuntimeError("evidence subsystem exploded")

        def note_gap(self, *a, **k):
            raise RuntimeError("evidence subsystem exploded")

    e = engine_factory(**live())
    e.evidence = Hostile()
    e._tick()
    broker.next_candle()
    e._tick()
    assert len(orders(broker)) == 20, "the grid was placed despite a hostile recorder"

    broker.price += 4.0
    broker.next_candle()
    e._tick()
    for o in list(orders(broker)):
        broker.cancel_pending_order(o.ticket)

    broker.price -= 30.0
    e._protective_tick()          # must not raise
    assert broker.get_open_positions("XAUUSD", magic=MAGIC) == [], (
        "a failing recorder prevented a protective exit"
    )


def test_the_real_recorder_never_raises_whatever_it_is_handed():
    rec = ev.SessionEvidence(manifest())

    class Unserializable:
        def __repr__(self):
            raise RuntimeError("even repr fails")

    assert rec.record("weird", thing=Unserializable()) is not None
    assert rec.coverage()["events_recorded"] == 1


def test_the_default_engine_records_nothing_and_costs_nothing(broker, engine_factory):
    """A session is optional; the bot runs without one."""
    e = engine_factory(**live())
    assert isinstance(e.evidence, ev.NullEvidence)
    e._tick()
    assert e.evidence.coverage()["events_recorded"] == 0


# --- the engine records the right events at the right points -----------------

def test_a_session_captures_admission_placement_and_quotes(broker, engine_factory):
    e = engine_factory(**live())
    e.evidence = ev.SessionEvidence(manifest())
    e._tick()
    broker.next_candle()
    e._tick()

    kinds = {x["kind"] for x in e.evidence.events}
    assert ev.ADMISSION_DECIDED in kinds
    assert ev.GRID_PLACED in kinds
    assert ev.QUOTE_OBSERVED in kinds

    placed = e.evidence.of_kind(ev.GRID_PLACED)[0]
    assert placed["orders_resting"] == 20
    assert placed["lot"] == 0.01 and placed["spacing"] == 0.30


def test_a_refused_admission_records_its_reason(broker, engine_factory):
    e = engine_factory(**live(), capital_floor_usd=100_000.0)
    e.evidence = ev.SessionEvidence(manifest())
    e._tick()
    broker.next_candle()
    e._tick()
    decisions = e.evidence.of_kind(ev.ADMISSION_DECIDED)
    assert decisions and decisions[-1]["allowed"] is False
    assert "capital floor" in decisions[-1]["reason"]


def test_a_loss_exit_records_the_limit_the_halt_and_the_confirmation(broker, engine_factory):
    # $40: a daily limit under the ~$37.80 completed-grid freeze is refused at
    # admission, so no grid would be placed and no limit could fire.
    e = engine_factory(**live(max_daily_loss_usd=40.0))
    e.evidence = ev.SessionEvidence(manifest())
    e._tick()
    broker.next_candle()
    e._tick()
    broker.price += 4.0
    broker.next_candle()
    e._tick()
    for o in list(orders(broker)):
        broker.cancel_pending_order(o.ticket)
    broker.price -= 7.0
    e._protective_tick()

    kinds = [x["kind"] for x in e.evidence.events]
    assert ev.LIMIT_EVENT in kinds, "the limit breach itself was not recorded"
    assert ev.HALT in kinds
    assert ev.CLOSE_INTENT_OPENED in kinds
    limit = e.evidence.of_kind(ev.LIMIT_EVENT)[0]
    assert "day_risk" in limit and limit["day_risk"]["marked_result_usd"] < 0


def test_an_unconfirmed_close_records_progress_not_completion(broker, engine_factory, monkeypatch):
    e = engine_factory(**live())
    e.evidence = ev.SessionEvidence(manifest())
    e._tick()
    broker.next_candle()
    e._tick()
    broker.price += 4.0
    broker.next_candle()
    e._tick()
    for o in list(orders(broker)):
        broker.cancel_pending_order(o.ticket)
    monkeypatch.setattr(broker, "close_position",
                        lambda t: (_ for _ in ()).throw(RuntimeError("rejected")))
    broker.price -= 30.0
    e._protective_tick()

    progress = e.evidence.of_kind(ev.CLOSE_INTENT_PROGRESS)
    assert progress and progress[-1]["confirmed_flat"] is False
    assert progress[-1]["positions_remaining"] > 0
    assert e.evidence.of_kind(ev.CLOSE_INTENT_DONE) == [], (
        "an unconfirmed close was recorded as done"
    )


def test_pause_and_resume_are_recorded(broker, engine_factory):
    e = engine_factory(**live())
    e.evidence = ev.SessionEvidence(manifest())
    e._tick()
    broker.next_candle()
    e._tick()
    e.pause_entries("owner paused for the runbook step")
    e.resume_entries()
    kinds = [x["kind"] for x in e.evidence.events]
    assert ev.PAUSE in kinds and ev.RESUME in kinds
    assert e.evidence.of_kind(ev.PAUSE)[0]["orders_cancelled"] == 20


def test_an_unreadable_broker_during_a_close_records_a_gap(broker, engine_factory, monkeypatch):
    e = engine_factory(**live())
    e.evidence = ev.SessionEvidence(manifest())
    e._tick()
    broker.next_candle()
    e._tick()
    broker.price += 4.0
    broker.next_candle()
    e._tick()
    held = broker.get_open_positions("XAUUSD", magic=MAGIC)
    assert held, "fixture: the basket must hold something to close"
    e._open_close_intent("owner_request", "runbook close")
    # The link drops while the close is being confirmed, so the engine cannot
    # prove the exposure is gone.
    monkeypatch.setattr(broker, "get_open_positions",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("link down")))
    outstanding = e._drive_close_intent(held, [])
    assert outstanding is True, "an unconfirmable close must stay outstanding"
    assert e.evidence.coverage()["coverage_gaps"] >= 1


def test_quotes_are_sampled_rather_than_recorded_every_tick(broker, engine_factory):
    """A quote per protective tick would bury the decisions in a bounded buffer."""
    e = engine_factory(**live())
    e.evidence = ev.SessionEvidence(manifest())
    for _ in range(30):
        e._protective_tick()
    quotes = e.evidence.of_kind(ev.QUOTE_OBSERVED)
    assert 1 <= len(quotes) <= 3, f"30 ticks recorded {len(quotes)} quotes"
    assert quotes[0]["sampled_every_seconds"] == e.QUOTE_EVIDENCE_INTERVAL_S


def test_a_change_in_what_is_open_is_recorded_immediately(broker, engine_factory):
    e = engine_factory(**live())
    e.evidence = ev.SessionEvidence(manifest())
    e._tick()
    broker.next_candle()
    e._tick()
    before = len(e.evidence.of_kind(ev.QUOTE_OBSERVED))
    broker.price += 4.0            # fills arrive: the picture changed
    e._protective_tick()
    after = e.evidence.of_kind(ev.QUOTE_OBSERVED)
    assert len(after) > before, "a change in open positions must not wait for the timer"
    assert after[-1]["positions_open"] > 0


def test_a_settlement_is_recorded_once_per_ticket(broker, engine_factory):
    e = engine_factory(**live())
    e.evidence = ev.SessionEvidence(manifest())
    e._tick()
    broker.next_candle()
    e._tick()
    broker.price += 4.0
    broker.next_candle()
    e._tick()
    e.close_and_pause("runbook close")
    e._settle_closed_trades("runbook close")     # a second sweep over the same tickets

    settlements = e.evidence.of_kind(ev.SETTLEMENT)
    tickets = [s["ticket"] for s in settlements]
    assert tickets, "closing a basket must record settlements"
    assert len(tickets) == len(set(tickets)), "a ticket was settled into evidence twice"


def test_a_revised_realised_figure_links_a_correction_and_keeps_the_original(broker, engine_factory):
    e = engine_factory(**live())
    e.evidence = ev.SessionEvidence(manifest())
    first = e.evidence.record(ev.SETTLEMENT, ticket="t-1", profit=-2.0)
    e._settlement_events["t-1"] = (first["event_id"], -2.0)

    e._record_settlement("t-1", -2.35, "basket stop")   # swap landed later

    corrections = e.evidence.of_kind(ev.CORRECTION)
    assert len(corrections) == 1
    assert corrections[0]["corrects_event_id"] == first["event_id"]
    assert corrections[0]["previous_profit"] == -2.0 and corrections[0]["profit"] == -2.35
    assert e.evidence.of_kind(ev.SETTLEMENT)[0]["profit"] == -2.0, \
        "the original settlement was rewritten instead of corrected"


def test_an_unchanged_figure_does_not_manufacture_a_correction(broker, engine_factory):
    e = engine_factory(**live())
    e.evidence = ev.SessionEvidence(manifest())
    first = e.evidence.record(ev.SETTLEMENT, ticket="t-1", profit=-2.0)
    e._settlement_events["t-1"] = (first["event_id"], -2.0)
    e._record_settlement("t-1", -2.0, "basket stop")
    assert e.evidence.of_kind(ev.CORRECTION) == []


def test_a_heartbeat_carries_exposure_risk_and_measured_cycle_time(broker, engine_factory):
    e = engine_factory(**live())
    e.evidence = ev.SessionEvidence(manifest())
    e._tick()
    broker.next_candle()
    e._tick()                                   # places the grid
    e._tick()                                   # the cycle that sees it resting

    beats = e.evidence.of_kind(ev.EXPOSURE_SNAPSHOT)
    assert beats, "a session must be able to show what was open"
    beat = beats[-1]
    assert beat["orders_resting"] == 20
    assert beat["day_risk"] is not None
    assert beat["protective_cycle_ms"]["count"] >= 1, "cycle time is measured, not guessed"


def test_the_heartbeat_is_throttled_while_nothing_changes(broker, engine_factory):
    e = engine_factory(**live())
    e.evidence = ev.SessionEvidence(manifest())
    e._tick()
    broker.next_candle()
    e._tick()
    e._tick()                                   # the grid is resting and recorded
    settled = len(e.evidence.of_kind(ev.EXPOSURE_SNAPSHOT))

    for _ in range(10):                         # same picture, same candle
        e._tick()
    assert len(e.evidence.of_kind(ev.EXPOSURE_SNAPSHOT)) == settled


def test_a_change_in_exposure_does_not_wait_for_the_timer(broker, engine_factory):
    e = engine_factory(**live())
    e.evidence = ev.SessionEvidence(manifest())
    e._tick()
    broker.next_candle()
    e._tick()
    before = len(e.evidence.of_kind(ev.EXPOSURE_SNAPSHOT))
    broker.price += 4.0                         # stops fill
    e._tick()
    beats = e.evidence.of_kind(ev.EXPOSURE_SNAPSHOT)
    assert len(beats) > before
    assert beats[-1]["positions_open"] > 0


def test_losing_and_regaining_the_link_is_recorded(broker, engine_factory, monkeypatch):
    e = engine_factory(**live())
    e.evidence = ev.SessionEvidence(manifest())
    e._tick()

    monkeypatch.setattr(broker, "is_connected", lambda: False)
    e._tick()
    assert e.evidence.coverage()["coverage_gaps"] >= 1, "a dropped link must be visible"

    monkeypatch.setattr(broker, "is_connected", lambda: True)
    e._tick()
    assert e.evidence.of_kind(ev.RECONNECT), "coming back must be recorded too"


def test_status_shows_what_the_broker_said_not_what_the_app_is_set_to(broker, engine_factory):
    e = engine_factory(**live())
    assert e.status()["broker_trade_mode"] == "unchecked", \
        "nothing has asked the broker yet"
    e.verify_account(broker.get_account_info())
    assert e.status()["broker_trade_mode"] == broker.get_account_info().trade_mode


def test_evidence_capture_is_inert_when_no_session_is_recording(broker, engine_factory):
    """The throttles and the heartbeat must cost nothing when nobody is recording."""
    e = engine_factory(**live())
    for _ in range(5):
        e._tick()
        broker.next_candle()
    assert e.evidence.of_kind(ev.EXPOSURE_SNAPSHOT) == []
    assert e._last_heartbeat_ms < 0, "the heartbeat clock moved without a session"


# --- shadow and simulated stay separate from fills ---------------------------

def test_a_shadow_prediction_is_not_an_executed_order():
    rec = ev.SessionEvidence(manifest(ai_mode="shadow"))
    rec.record(ev.SHADOW_PREDICTION, predicted_net=12.0, abstained=False,
               executed=False, simulated=True)
    rec.record(ev.GRID_PLACED, basket_id="b1", orders_resting=20)

    shadow = rec.of_kind(ev.SHADOW_PREDICTION)[0]
    assert shadow["executed"] is False and shadow["simulated"] is True
    assert shadow["kind"] != ev.GRID_PLACED
    assert rec.manifest.ai_mode == "shadow", "the actual AI mode must be on record"


def test_the_manifest_records_the_ai_mode_that_was_actually_running():
    assert manifest(ai_mode="disabled").as_dict()["ai_mode"] == "disabled"
    assert manifest(ai_mode="shadow").as_dict()["ai_mode"] == "shadow"


# --- packet writing -----------------------------------------------------------

def test_the_packet_writes_atomically_and_reloads(tmp_path):
    rec = ev.SessionEvidence(manifest(), directory=tmp_path)
    rec.record(ev.QUOTE_OBSERVED, price=4000.0)
    path = rec.write_packet(tmp_path / "packet.json")
    reloaded = json.loads(path.read_text())
    assert reloaded["manifest"]["profile_key"] == "baseline@v1"
    assert len(reloaded["events"]) == 1
    assert list(tmp_path.glob("*.tmp")) == [], "a temporary file was left behind"


# --- the export tool and its refusal to write a leak -------------------------
#
# The guard is the only thing standing between a recorded session and an email
# containing a broker password, so it is tested from both sides: it must not
# fire on clean output, and it must fire on a secret however it is nested.

def write_session(directory, *, events, manifest_extra=None):
    session_id = "sess-testfixture"
    body = {"schema_version": 1, "session_id": session_id,
            "started_at_utc": "2026-09-22T00:00:00+00:00",
            "profile_key": "baseline@v1", "symbol": "XAUUSD"}
    body.update(manifest_extra or {})
    (directory / f"{session_id}.manifest.json").write_text(json.dumps(body))
    with (directory / f"{session_id}.events.jsonl").open("w") as handle:
        for index, event in enumerate(events, start=1):
            handle.write(json.dumps({"seq": index, "event_id": f"{session_id}-{index:08d}",
                                     "at_utc": "2026-09-22T00:00:0%d+00:00" % index,
                                     **event}) + "\n")
    return session_id


def test_the_scan_does_not_fire_on_its_own_redacted_output():
    """The regression this test exists for.

    An earlier guard asserted the value with `\\s*(?!"<redacted>")`. Because
    `\\s*` can backtrack to nothing, the lookahead was evaluated against the
    space and succeeded, so a correctly redacted packet was reported as a leak.
    The scan now reads the value instead of asserting around it.
    """
    packet = ev.redact({"mt5_login": 12345678, "mt5_password": "hunter2",
                        "api_key": "sk-live-x", "account_id": "12345678"})
    text = json.dumps(packet, indent=2)
    assert export_session.scan(text, packet) == []


def test_the_scan_catches_a_secret_that_was_never_redacted():
    leaky = {"manifest": {"mt5_login": 12345678, "mt5_password": "hunter2"}}
    problems = export_session.scan(json.dumps(leaky), leaky)
    assert len(problems) >= 2
    assert any("mt5_password" in p for p in problems)


def test_the_scan_catches_a_secret_hidden_inside_a_logged_string():
    """The structural walk cannot see this one; the text backstop must."""
    packet = {"events": [{"kind": "coverage_gap",
                          "reason": 'login failed: {"mt5_password": "hunter2"}'}]}
    problems = export_session.scan(json.dumps(packet), packet)
    assert any("mt5_password" in p for p in problems)


def test_a_number_under_a_login_key_is_not_treated_as_redacted():
    packet = {"mt5_login": 0}
    assert export_session.scan(json.dumps(packet), packet) != []


def test_exporting_a_clean_session_writes_a_packet_without_the_secrets(tmp_path):
    session_id = write_session(
        tmp_path,
        manifest_extra={"mt5_login": 12345678, "mt5_password": "hunter2",
                        "mt5_server": "Exness-MT5Trial9", "account_id": "12345678",
                        "strategy_config": {"database_url": "sqlite:///C:\\Users\\Home\\x.db"}},
        events=[{"kind": ev.QUOTE_OBSERVED, "bid": 4000.0, "ask": 4000.3},
                {"kind": ev.CLOSE_INTENT_OPENED, "basket_id": "b-1"},
                {"kind": ev.CLOSE_INTENT_DONE, "basket_id": "b-1"}])
    out = tmp_path / "packet.json"
    code = export_session.main_with(["--dir", str(tmp_path), "--session", session_id,
                                     "--out", str(out)])
    assert code == 0, "a correctly redacted session must export"
    text = out.read_text()
    for secret in ("hunter2", "12345678", "Exness-MT5Trial9", "C:\\Users"):
        assert secret not in text, f"{secret} reached the shareable packet"
    packet = json.loads(text)
    assert packet["manifest"]["account_id"].startswith("acct_")
    assert packet["summary"]["close_intents_unconfirmed"] == 0


def test_the_export_refuses_to_write_when_a_secret_survives(tmp_path):
    session_id = write_session(
        tmp_path,
        events=[{"kind": ev.COVERAGE_GAP,
                 "reason": 'connect failed: {"mt5_password": "hunter2"}'}])
    out = tmp_path / "packet.json"
    code = export_session.main_with(["--dir", str(tmp_path), "--session", session_id,
                                     "--out", str(out)])
    assert code == 4
    assert not out.exists(), "a packet that failed the scan must not be on disk"


def test_the_export_counts_a_torn_final_line_rather_than_dropping_it(tmp_path):
    session_id = write_session(tmp_path, events=[{"kind": ev.QUOTE_OBSERVED, "bid": 4000.0}])
    with (tmp_path / f"{session_id}.events.jsonl").open("a") as handle:
        handle.write('{"seq": 2, "kind": "tor')
    data = export_session.load_session(tmp_path, session_id)
    assert data["malformed_lines"] == 1
    assert len(data["events"]) == 1


def test_an_unconfirmed_close_is_reported_as_unconfirmed():
    summary = export_session.summarize([
        {"kind": ev.CLOSE_INTENT_OPENED, "at_utc": "t1"},
        {"kind": ev.CLOSE_INTENT_PROGRESS, "at_utc": "t2"},
    ])
    assert summary["close_intents_unconfirmed"] == 1, \
        "a close that was sent but never confirmed must not read as finished"
