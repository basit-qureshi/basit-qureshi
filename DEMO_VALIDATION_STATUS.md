# Demo validation status — Phase F

Branch `claude/forex-ai-trading-bot-izgn6l`. Source revision: `6771366`
(Phase F), on top of `e9f1e21` (Phase E). **Nothing has been pushed;
`origin` is still at `1116af1`.** No terminal was connected, no order was
placed, no session was recorded on a real account.

## The four questions, answered separately

| Question | Answer |
| --- | --- |
| Do the **offline checks** pass? | **Yes.** 336 backend tests, 4 frontend tests, lint clean, build clean, and a fixture dry-run that records a full session and exports it. |
| Is a **demo session ready to run**? | **Prepared, not authorized to start.** The collector, the export and the procedure exist. It is blocked on three owner risk numbers (`OWNER_RUNBOOK.md` §E). |
| Has an **actual session occurred**? | **No.** Zero sessions recorded. Zero demo trades. Zero live trades. |
| Is **profitability supported**? | **No. Trading performance is not measured.** There is no market data and no session data, so there is nothing to measure it from. |

These stay four answers. Collapsing them into one word is how a working
collector gets read as a working strategy.

---

## 1. What Phase F added

Phase E left the bot verifiable but unobservable: after a session you had a
dashboard and a database, and no way to answer "what did it actually do, and
when did it know it" from a file. This phase adds that record.

| Piece | Where | What it is for |
| --- | --- | --- |
| Session manifest | `app/evidence/session.py` `build_manifest` | Pins revision, frozen profile, strategy config, symbol, accounting timezone, AI mode, starting capital and **verified** account type |
| Append-only event log | `SessionEvidence` | Decisions with reasons, quotes, close intents, settlement, limits, halts, pauses, link loss, measured cycle time |
| Corrections that link | `SessionEvidence.correct` | A revised figure appends a row pointing at the row it revises; the original is never edited |
| Bounded capture | capacity + `dropped_events` + `storage_errors` | A long session cannot grow without limit, and what was lost is counted rather than silent |
| Redaction | `redact`, `is_sensitive_key` | Passwords, logins, server names, tokens and database paths never reach the shareable copy; the account id becomes a stable `acct_…` reference so files still reconcile |
| Export with a refusal | `tools/export_session.py` | Scans the finished packet and **refuses to write it** if anything private survived, naming the events to look at |
| Session endpoints | `/api/session/{start,stop,status,packet}` | Starting a recording is not starting the bot; stopping one is not stopping the bot |
| Fixture dry-run | `tests/bench/collector_dryrun.py` | Proves the collector and the export work end to end with no broker |

**Not recorded, deliberately:** per-order request/response round trips. The
protective path must not pay for capture, and the broker adapter is where that
belongs. Fills reach the record through `settlement` and the exposure
heartbeat, and a close is only ever reported as confirmed when the broker says
the position is gone.

### The separation this record exists to keep

`request_sent`, `response_received` and `state_confirmed` are three different
event kinds in the schema, reserved for the adapter and **not emitted by the
engine today**. What the engine does emit keeps the same distinction where it
matters most: `close_intent_opened` is not `close_intent_done`, and only the
broker reporting nothing open produces the second. A packet with
`close_intents_unconfirmed > 0` is a session where something was asked to close
and never confirmed gone — the export prints that line whether it is zero or
not, so it cannot be omitted by accident.

---

## 2. Defects found and fixed this phase

| Defect | Why it mattered | Fix | Regression |
| --- | --- | --- | --- |
| **`_close_everything` read the broker unguarded at three points.** A link failure during a close raised out of the close path and killed the protective cycle — at the exact moment the account had open exposure and a stop had just fired. | The one path that must survive a bad link was the one that did not | All three reads go through `_safe_positions`/`_safe_pendings`; `None` means "could not confirm", the intent stays outstanding, a coverage gap is recorded and `_last_error` says exposure is not confirmed gone | `test_an_unreadable_broker_during_a_close_records_a_gap` |
| **The export's redaction scan fired on its own correct output.** The guard asserted the value with `\s*(?!"<redacted>")`; `\s*` backtracks to nothing, so the lookahead was evaluated against the space and succeeded. A correctly redacted packet was reported as a leak — and a guard that cries wolf is a guard that gets bypassed. | An unusable safety check is worse than none, because it trains you to ignore it | The scan reads the value and compares it, structurally over the packet and textually over the serialized form | `test_the_scan_does_not_fire_on_its_own_redacted_output` and three more |
| **A second `/api/session/start` silently replaced a live recorder.** One run's evidence would end up split across two files with neither reconcilable. | Evidence you cannot reconcile is not evidence | The second start refuses and names the running session; `replace=true` is explicit | `test_a_second_start_refuses_rather_than_splitting_the_evidence` |
| **The manifest's AI mode was hardcoded at the call site**, exactly the mistake the `account_type` field exists to prevent. | A manifest that states a fact it did not check is worse than one that omits it | Both `/api/ai-status` and the manifest read `current_ai_mode()`; it is `disabled` because the engine contains no call site that consults a model | `test_a_recording_over_a_mock_broker_is_never_labelled_verified` |

Two smaller things were corrected while wiring capture: every recorder call
from the engine goes through `_record_evidence`/`_note_evidence_gap`, so a
hostile or broken recorder cannot raise into a protective cycle; and quotes are
sampled every 15 s rather than every protective tick, so a day-long session
cannot push its own decisions out of a bounded buffer.

---

## 3. Verification actually performed

Linux container, Python 3.11. No MT5, no Windows, no terminal, no network
provider, no credentials read.

```
cd backend  && python3 -m pytest -q                        -> 336 passed
cd backend  && python3 -m compileall -q app tools          -> OK
cd backend  && python3 -m tests.bench.collector_dryrun     -> rc 0
cd frontend && npm test                                    -> 4 passed
cd frontend && npm run lint                                -> 0 errors, 4 warnings (pre-existing)
cd frontend && npm run build                               -> clean
```

The dry-run records a full lifecycle against the fake broker and exports it:

```
admission_decided 1 · grid_placed 1 · quote_observed 2 · exposure_snapshot 3
coverage_gap 1 · reconnect 1 · limit_event 1 · halt 1
close_intent_opened 1 · settlement 10 · close_intent_done 1 · correction 1
coverage: recorded=24 dropped=0 storage_errors=0 complete=True
packet: 24 events, close intents 1 opened / 1 confirmed flat, 1 correction
redaction scan passed
```

Those are **fixture prices and simulated fills.** They establish that the
collector works. They establish nothing about gold, this strategy, or money.

**Evidence classes, kept apart:**

| Class | Present |
| --- | --- |
| Synthetic contract tests | **yes** — 336 backend, 4 frontend |
| Fixture collector dry-run | **yes** — one, reproducible |
| Historical replay on real ticks | **no** — no dataset exists |
| Recorded demo session | **no** — none has been run |
| Live account evidence | **no**, and not authorized |

---

## 4. Session analysis

**There is no session data to analyse.** No manifest, no event log, no packet,
no reconciliation against a broker export. Net result, marked drawdown, worst
observed loss, limit overshoot, basket counts, execution failures, time to
confirmed closure, turnover and residual exposure are all **not measured** —
not "zero", not "pending", not measured.

**Trading performance is not measured.** Nothing in this repository currently
supports a claim about profit.

### The exact minimum collection request

Two independent things are missing, and they unblock different questions.

**(a) Tick history — unblocks the only question that matters first.**
`OWNER_RUNBOOK.md` §B1 has the command. Columns `time_utc,bid,ask`, **bid and
ask on every row**, the exact broker symbol including its suffix, tick
resolution not M1 bars, at least 3 months continuous. One file. With it, the
target-versus-freeze ratio from Phase C becomes measurable; without it, every
performance question stays unanswerable no matter how many demo sessions run.

**(b) One recorded demo session — unblocks the operational questions.**
`OWNER_RUNBOOK.md` §D covers it. It verifies that the bot does what it says on
a real terminal: identity, ownership, truthful exposure, pause, closure,
recovery. It does **not** measure edge, and a profitable demo week would not
change that.

Do (a) first if you have to choose. A demo session tells you the machinery
works; the ticks tell you whether the strategy is worth running it for.

---

## 5. Unresolved, unchanged from Phase E

1. **No market data.** Blocks every performance claim.
2. **No broker-side stop loss.** Every protection in this system runs inside
   the Python process. If it stops, nothing closes anything. A loss threshold
   is a trigger, not a guaranteed fill through a gap. Still an owner decision:
   the original specification asked for per-trade SL, the locked strategy
   forbids it.
3. **Three owner risk numbers unset.** `GRID_CAPITAL_FLOOR_USD = 0` blocks new
   entries by design, and a basket stop below ≈ $37.80 makes admission refuse
   the grid outright — as the dry-run demonstrates.
4. **No Windows or broker verification of anything.**
5. **Evidence capture itself is unverified on a real terminal.** It has been
   verified against fixtures only. The first recorded session is also the first
   test of the recorder under real conditions.

---

## 6. What to send for independent review

- `DEMO_VALIDATION_STATUS.md` (this file)
- `PHASE_E_ACCEPTANCE.md` — the acceptance ledger, not repeated here
- `OWNER_RUNBOOK.md` — the procedure, including §F for recording a session
- `backend/app/evidence/session.py` and `backend/tools/export_session.py`
- `backend/tests/test_phase_f_evidence.py`

After a session exists, add the **redacted packet** from
`tools\export_session.py`. Never the `evidence\` folder itself — that one is
unredacted by design.
