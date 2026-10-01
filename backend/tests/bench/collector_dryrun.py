"""Drive a whole recorded session against the FAKE broker, then export it.

This exists because there is no demo session data. It cannot tell you anything
about trading results — the prices are a fixture and the fills are simulated —
but it does answer the question the owner has to settle before connecting a
terminal: **does the collector actually record a session, and does the export
actually produce a file with nothing private in it?**

What it runs:

  1. a basket placed, filled, and flattened when the day's loss limit is hit
  2. a settlement figure revised afterwards, which must LINK a correction
  3. a broker link that drops and comes back
  4. the redaction scan over the finished packet

It touches no terminal, opens no network connection and reads no credentials.
Everything is written under a temporary directory and removed afterwards.

Run:  python3 -m tests.bench.collector_dryrun
      .\\venv\\Scripts\\python.exe -m tests.bench.collector_dryrun
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

os.environ.setdefault("BROKER_MODE", "mock")
os.environ.setdefault("SYMBOL", "XAUUSD")
os.environ.setdefault("ACCOUNT_TYPE", "demo")
os.environ.setdefault("DATABASE_URL", "sqlite:///" + tempfile.mktemp(suffix=".db"))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db as db_module  # noqa: E402
from app.engine.grid_engine import GridEngine  # noqa: E402
from app.evidence import session as ev  # noqa: E402
from tests.conftest import MAGIC, FakeBroker  # noqa: E402
from tools import export_session  # noqa: E402


def run() -> int:
    db_module.init_db()
    workspace = tempfile.mkdtemp(prefix="collector-dryrun-")
    try:
        return _run(workspace)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def _run(workspace: str) -> int:
    broker = FakeBroker()
    # Fixture values, NOT a recommendation. Both the basket stop AND the daily
    # loss limit have to exceed the ~$37.80 a completed 10+10 grid locks in, or
    # admission refuses to place one at all — which is itself the protection.
    engine = GridEngine(broker=broker, symbol="XAUUSD", mode="demo", magic_number=MAGIC,
                        capital_floor_usd=50.0, basket_stop_loss_usd=60.0,
                        max_daily_loss_usd=40.0, basket_take_profit_usd=10_000.0)

    manifest = ev.build_manifest(
        profile_key="baseline@v1",
        strategy_config={"grid_lot_size": 0.01, "grid_distance": 0.30,
                         "grid_buy_levels": 10, "grid_sell_levels": 10},
        symbol="XAUUSD", accounting_timezone="Asia/Karachi", ai_mode="disabled",
        account_info=broker.get_account_info(),
        account_verified=False,            # a FAKE broker is never verified
    )
    engine.evidence = ev.SessionEvidence(manifest, directory=workspace)
    print(f"session {manifest.session_id}")
    print(f"  account_type : {manifest.account_type}   <- a fixture is never 'verified'")
    print(f"  ai_mode      : {manifest.ai_mode}")
    print(f"  profile      : {manifest.profile_key} (frozen: {manifest.profile_frozen})")

    engine._tick()                                   # first look
    broker.next_candle()
    engine._tick()                                   # grid goes out
    broker.price += 4.0                              # buy stops fill
    broker.next_candle()
    engine._tick()

    # The link drops and returns while the basket is live.
    real_is_connected = broker.is_connected
    broker.is_connected = lambda: False
    engine._tick()
    broker.is_connected = real_is_connected
    engine._tick()

    # Price turns back through the filled side until the day's limit is reached.
    for order in list(broker.get_pending_orders("XAUUSD", magic=MAGIC)):
        broker.cancel_pending_order(order.ticket)
    broker.price -= 7.0
    engine._protective_tick()
    engine._reporting_tick()

    # A realised figure that moves after settlement must link a correction.
    for ticket, (event_id, profit) in list(engine._settlement_events.items())[:1]:
        engine._record_settlement(ticket, (profit or 0.0) - 0.35, "swap posted late")

    coverage = engine.evidence.coverage()
    kinds: dict[str, int] = {}
    for event in engine.evidence.events:
        kinds[event["kind"]] = kinds.get(event["kind"], 0) + 1

    print("\nrecorded:")
    for kind, count in sorted(kinds.items()):
        print(f"  {kind:<24} {count}")
    print(f"\ncoverage: recorded={coverage['events_recorded']} "
          f"dropped={coverage['dropped_events']} "
          f"storage_errors={coverage['storage_error_count']} "
          f"gaps={coverage['coverage_gaps']} complete={coverage['complete']}")

    out = os.path.join(workspace, "packet.json")
    code = export_session.main_with(["--dir", workspace, "--session", manifest.session_id,
                                     "--out", out])
    if code != 0:
        print("\nFAILED: the export refused to write the packet", file=sys.stderr)
        return 1

    packet = json.loads(open(out, encoding="utf-8").read())
    text = json.dumps(packet)
    problems = []
    if "close_intent_opened" not in kinds:
        problems.append("no close intent was recorded")
    if kinds.get("close_intent_done", 0) < 1:
        problems.append("no confirmed-flat close was recorded")
    if kinds.get("correction", 0) < 1:
        problems.append("a revised settlement did not link a correction")
    if kinds.get("coverage_gap", 0) < 1:
        problems.append("a dropped link was not recorded as a gap")
    if kinds.get("reconnect", 0) < 1:
        problems.append("a restored link was not recorded")
    if packet["manifest"]["account_type"] != "unverified":
        problems.append("a fixture session claimed a verified account")
    for secret in ("password", "mt5_login"):
        if f'"{secret}": "<redacted>"' not in text and secret in text:
            problems.append(f"{secret} appears unredacted")

    print(f"\npacket   : {os.path.getsize(out)} bytes, {packet['summary']['events']} events")
    print(f"  close intents opened / confirmed flat : "
          f"{packet['summary']['close_intents_opened']} / "
          f"{packet['summary']['close_intents_confirmed_flat']}")
    print(f"  corrections                           : {packet['summary']['corrections']}")

    if problems:
        print("\nFAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print("\nThe collector records a full lifecycle and the export refuses to leak.")
    print("This says NOTHING about trading results: the prices are a fixture.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
