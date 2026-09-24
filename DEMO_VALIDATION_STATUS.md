# Demo validation status — Phase F, amended by the A–F audit

Branch `claude/forex-ai-trading-bot-izgn6l`. Source revision: `6771366`
(Phase F) plus the audit commit below, on top of `e9f1e21` (Phase E).
**Nothing has been pushed; `origin` is still at `1116af1`.** No terminal was
connected, no order was placed, no session was recorded on a real account.

§7 holds the A–F audit and the corrections that followed it: §7.3 withdraws
three claims the earlier report made about the risk figure, §7.5 corrects the
performance and phase labels, §7.7 splits status into implementation,
integration, evidence, connected verification and activation.

## The four questions, answered separately

| Question | Answer |
| --- | --- |
| Do the **offline checks** pass? | **Yes.** 388 backend tests, 4 frontend tests, lint clean, build clean, and a fixture dry-run that records a full session and exports it. Offline, against fake brokers — see §7.8 for what that does and does not establish. |
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
cd backend  && python3 -m pytest -q                        -> 388 passed
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
| Synthetic contract tests | **yes** — 388 backend, 4 frontend |
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

---

## 7. Phase A–F audit — verified against the code, not the reports

Each phase was re-checked by reading the implementation and running probes
against the fake broker, on the assumption that a phase report proves nothing
about the code. Two defects survived into this pass; both are fixed below.

### 7.1 Status by phase

| Phase | Claim | Audit finding |
| --- | --- | --- |
| A — capital protection | marked daily risk, durable closure, real admission | **Implemented, and two holes found this pass** (7.2). The marked identity, the anchor's survival across restarts and the three owner invariants hold: `app/engine/risk_accounting.py`, `tests/test_phase_a_acceptance.py` |
| B — execution | reporting off the protective path | **Implemented and measured.** Re-run this pass: full tick median **84.3 ms**, protective-only **25.9 ms**, a 69% reduction on the synthetic bench. `tests/bench/bench_execution.py` |
| C — strategy | evidence-based selection | **Blocked by data, not implemented-and-failing.** The harness runs and refuses to select: `SELECTION RESULT: INSUFFICIENT EVIDENCE — BLOCKED BY DATA`. `tests/bench/run_phase_c.py` |
| D — AI / news | auditable pipeline, shadow only | **Implemented, unwired by design.** No model exists, and the engine contains no call site that consults one — `test_ai_shadow_cannot_mutate_orders_or_configuration` greps the engine source to keep it that way |
| E — validation | integration verification | **Holds.** Both Phase E fixes are still in place: the halt check in `_entry_gate:1275`, the retired poll knob in `status()` |
| F — evidence | session record and redacted export | **Implemented and verified against fixtures.** Still zero recorded sessions |

### 7.2 Defects found by the audit and fixed

| # | Defect | Evidence | Fix |
| --- | --- | --- | --- |
| 1 | **A grid was admitted whose completed-grid estimate exceeded the remaining daily budget or the capital-floor headroom.** Admission already applied that comparison to the *basket* budget and to nothing else. Probe: with $15 left of a $100 daily budget, a grid whose completed-grid estimate was $37.80 (fixture inputs) was admitted | `_affordability`, `app/engine/grid_engine.py` | Refuse when the estimate exceeds the remaining daily allowance or the balance-minus-floor headroom. Both budgets are the owner's own settings and the estimate is the engine's own calculation; nothing is invented. `test_a_grid_that_cannot_fit_the_remaining_daily_budget_is_refused`, `test_a_grid_that_would_break_the_capital_floor_is_refused`, boundary tests either side of the comparison, and `test_the_same_grid_is_allowed_while_the_budget_can_absorb_it` so the guard cannot simply block everything |
| 2 | **A halted engine ignored exposure that appeared after its own close confirmed.** Only the profit path retired a finished close intent, so after a stop or a risk halt a DONE intent stayed attached for good — and `_check_risk_limits` re-opens a close for live exposure **only when no intent is attached**. Probe: halted, one position open at the broker with the bot's own magic number, one protective tick, **position still open** | `_drive_close_intent`, `app/engine/grid_engine.py:838` | The intent is retired where it completes, so every cause behaves like the profit path. `test_a_completed_close_releases_its_intent`, `test_a_halted_engine_still_closes_exposure_that_appears_afterwards` |

### 7.3 Correction to the previous report's risk claim

The previous report said every budget "must exceed approximately $37.80". That
sentence was wrong in three ways, and this section replaces it.

**It is not a universal figure.** `$37.80` is what *this project's test fixture*
produces: price 4000, `pip_size` 0.01, `pip_value_per_lot` 1.00, spread 0.24,
minimum stop distance 0. The code hardcodes none of that — every input comes
from the adapter — so a different symbol specification gives a different number.
On MT5 the adapter derives the minimum stop distance from the live spread
(`max((stops_level+5)*point, spread*3)`), and that distance pushes **every**
level further out:

| spread | min stop distance | first step | estimate |
| --- | --- | --- | --- |
| 0.24 | 0.00 (the fixture) | 0.30 | **37.80** |
| 0.25 | 0.75 | 0.75 | **47.00** |
| 0.30 | 0.90 | 0.90 | **51.00** |
| 0.50 | 1.50 | 1.50 | **67.00** |

Reproduce any row offline, with no terminal:
`python3 tools/grid_fit_report.py --spread 0.25 --min-stop-distance 0.75`.

**It is not a proven maximum loss.** It is the marked value of **one scenario**:
every configured level fills at exactly its own price, the two sides end with
equal volume, the entry spread is paid once per fill, and the basket is valued as
if closed back at the reference price. Named exclusions, now carried in code as
`grid_math.EXCLUDED_FROM_ESTIMATE` and printed by the report:

| Scenario / cost | In the estimate? |
| --- | --- |
| Fully filled, equal volume, valued at the reference | **yes — this is the scenario** |
| Entry spread, once per fill | yes |
| Exit spread, commission, slippage | **no** |
| Swap | **no** |
| Partial or unequal fills | **no** — the sides do not cancel |
| One-directional (trending) exposure | **no** — not frozen; bounded by the basket stop |
| Cancellation race, rejected close, gap during liquidation | **no** |
| Spread or minimum stop distance moving after admission | **no** — both read once |
| Conversion drift on a non-account-currency symbol | **no** — `pip_value_per_lot` is a snapshot |

MT5's `order_calc_profit` would also be an estimate of a specified operation in
account currency, not a worst-case path loss and not a settlement figure. It was
not called: no terminal was contacted in this task.

**The refusal is a policy, not a forecast.** The earlier wording said such a
basket "could only end by forcing the daily limit to liquidate it". That is
withdrawn. A basket refused on these grounds might well have reached its profit
target first. The ground for refusal is the declared admission policy — do not
open a basket whose named adverse scenario is larger than the budget that would
have to absorb it — and the refusal message now says exactly that, with a test
asserting it contains no forecast.

**Units and double counting.** Both sides of the comparison are account currency
(the settings are named `_usd`, which is a misnomer on a non-USD account; they
are whatever the account is denominated in). Nothing is counted twice:
`_consider_entry` only reaches admission with **zero** open positions, **zero**
resting orders and **zero** unsettled closes for this account/symbol/magic, so
the estimate is the whole of the new exposure and `marked_result` carries none of
it. Neither side carries a closing-cost buffer: `marked_result` deliberately
excludes the exit reserve (that is `risk_reading`), and the estimate excludes
exit costs.

**Withdrawn: the baskets-per-day table.** The previous report presented "a $100
daily limit holds 2 frozen baskets" as capacity. Dividing a budget by one
scenario is arithmetic under restricted assumptions, not a permitted trade count
and not a loss guarantee. It is gone from the runbook.

**Withdrawn: "fully enforceable".** These changes improve **prevention** (a
configuration whose adverse scenario cannot fit is refused before exposure
exists) and **recovery** (a halted engine now manages exposure that appears
after its own close). A limit is still a trigger: a gap, a rejected close, a
terminal that stops answering, or movement between two protective cycles can
still overshoot it.

**If the grid does not fit your limits, the rejection stands.** Nothing here
recommends raising a tolerated loss or funding an account to clear a gate.
`tools/grid_fit_report.py --alternatives` prints smaller profiles as arithmetic
for a decision you make later; they are untested, not active, and not known to
be better. A smaller grid has a smaller adverse scenario *and* a smaller cash
target, and which way that trade lands is a question for tick history.

### 7.4 Tests: what changed and what was added

**Why six budgets moved to $40.** Each of those tests drives the daily limit to
*fire*, and each used a limit ($5 or $15) smaller than the fixture's $37.80
estimate — a configuration admission now refuses before a grid exists, leaving
nothing to fire. $40 is the smallest round figure above the fixture estimate, so
each test still exercises the same path. Coverage of the smaller-budget case was
not lost; it was moved into tests that assert the **refusal**:

| Test | What it pins |
| --- | --- |
| `test_a_grid_that_cannot_fit_the_remaining_daily_budget_is_refused` | $15 remaining, refused |
| `test_an_estimate_exactly_equal_to_the_remaining_budget_is_allowed` | boundary: equal fits |
| `test_one_cent_less_budget_than_the_estimate_is_refused` | boundary: one cent under refuses |
| `test_the_refusal_states_a_policy_rather_than_predicting_the_outcome` | the message forecasts nothing |
| `test_the_same_grid_is_allowed_while_the_budget_can_absorb_it` | the guard does not block everything |
| `test_protection_over_preexisting_exposure_needs_no_admission` | exposure **constructed directly** under a configuration admission would refuse, then protected |

That last one answers the follow-up's point directly: protection over
pre-existing exposure no longer depends on admission being willing to create it.

**Close-intent retirement** now has its own file, `tests/test_close_lifecycle.py`
(9 tests): an unfinished close keeps its intent and keeps entries shut; an
unconfirmable close is not retired; an order surviving a cancellation race keeps
the close open; a confirmed close is retired and counted exactly once across
repeated cycles; a loss exit stays latched and entries do not resurrect;
settlement rows, realised figures and close reasons survive retirement; a
restart after completion does not resurrect a finished request; and a manual
trade on another magic number is never touched.

One of those tests failed when first written, and the fix was to the test, not
the code: a position appearing after a **basket stop** must be *managed* (marked,
valued, closed when a limit fires), not closed on sight. Immediate closure is the
**halted** case, which is covered separately. That distinction is now written
into the test name and docstring.

**New: `tests/test_grid_math.py`** (11 tests) checks the estimate against hand
arithmetic shown in each docstring, including a forex-shaped specification that
produces 320.00 from the same code, that doubling `pip_value_per_lot` doubles the
result, that a 0.75 minimum stop distance gives 47.00, and that an unreadable
symbol specification returns 0.00 rather than a guess. It also asserts the
engine, the gate and the offline report all use one implementation — the level
arithmetic was duplicated in `_build_grid`, which is now the same call, so the
gate cannot price a grid the engine would not place.

### 7.5 Corrected performance labels

**The Phase C `net` column was ambiguous and is now four columns.** `net_result`
sums **closed** baskets only; `remaining_exposure_marked` is what is still open.
The two sets are disjoint, so they add. Commission is inside both. Re-run:

```
profile                        done  realised open  open mkd  TOTAL mkd    comm blocked
baseline                          0      0.00    1    -24.58     -24.58    0.99       0
research-execution-quality        0      0.00    1    -24.58     -24.58    0.99       0
research-regime                   1     10.05    1    -33.46     -23.41    1.54     601
research-event-blackout           0      0.00    0      0.00       0.00    0.00    5700
research-trailing-exit            0      0.00    1    -24.58     -24.58    0.99       0
research-execution-and-regime     1     10.04    1    -35.82     -25.78    1.54     625
```

The follow-up's arithmetic was right: `research-regime` is **−23.41** total
marked against baseline's **−24.58**, i.e. **1.17 less loss**, both negative, one
basket, one synthetic fixture. The corrected table also shows something the
earlier report missed: `research-execution-and-regime` at **−25.78** is *worse*
than baseline. Strategy selection stays **BLOCKED BY DATA**. Cost assumptions are
now printed above the table (commission 2.75/lot/side, swap **not modelled**,
slippage 0, latency 0, spread paid through the quoted side on entry and exit).

**The timing figures are a synthetic benchmark of this process's own loop.**
84.3 ms full tick versus 25.9 ms protective-only, measured on
`tests/bench/bench_execution.py` in this Linux container, against in-process
doubles with stated per-call delays (account 5 ms, positions 12 ms, candles
25 ms, history sweep 120 ms). That is **not** broker fill latency, **not**
time-to-close a basket, and **not** anything observed on Windows.

**AI is an offline scaffold, not an integration.** The engine contains no call to
`decide()` and no `EntryPredictor`: **production shadow wiring is PENDING.** The
runtime evidence for "a model changes nothing" is no longer a grep — it is
`tests/test_ai_shadow_integration.py`, which drives a deliberately hostile fake
inference component (one that rejects everything, one that accepts everything,
one that raises) through `decide()` in SHADOW mode alongside a live engine and
compares the actual order prices and the configuration snapshot against a control
run with no model at all. A grep assertion remains, but only to record the
absence of wiring so it cannot close silently. No model was trained and no AI was
enabled.

### 7.6 Data on hand, and what it is not

Checked, not assumed: there is **no** market data in this environment. No `.csv`,
no `.db`, no `.parquet`, no `data/` directory, no tick export, no trade-history
export. The uploads in this session are prompt documents plus the TWP preset
archive (EA `.set` files, not price history). The trading CSV the owner supplied
earlier is **not present here** — and a trade-history export could help
reconcile results anyway, but it cannot substitute for tick history: it contains
fills, not the quotes between them.

June–September is a **starting collection window**, not evidence of sufficiency,
and nothing here assumes the broker retains that much tick history. Export what
exists, send the manifest, and let the coverage decide what can be measured.

### 7.7 Status, split the way the follow-up asks

| | Implementation | Integration | Historical evidence | Connected verification | Activation |
| --- | --- | --- | --- | --- | --- |
| A capital protection | done | in the live path | none | **not done** | n/a |
| B execution separation | done | in the live path | synthetic bench only | **not done** | n/a |
| C strategy selection | harness done | replay only, not live | **none — blocked by data** | **not done** | **no candidate** |
| D AI / news | **offline scaffold** | **PENDING — no engine call site** | none, no model trained | **not done** | **not authorized** |
| E validation | done | — | — | **not done** | n/a |
| F session evidence | done | wired into the engine | **fixture-verified only** | **not done** | n/a |

"Fixture-verified only" is the honest ceiling for F: zero sessions have been
recorded, so the collector has never run against a terminal.

### 7.8 Test results, exactly as they ran

```
backend  python3 -m pytest -q                     -> 388 passed
backend  python3 -m compileall -q app tools       -> OK
backend  python3 -m tests.bench.collector_dryrun  -> rc 0
backend  python3 -m tests.bench.run_phase_c       -> BLOCKED BY DATA (corrected table)
backend  python3 -m tests.bench.bench_execution   -> 84.3 ms full / 25.9 ms protective
frontend npm test                                 -> 4 passed
frontend npm run lint                             -> 0 errors, 4 pre-existing warnings
frontend npm run build                            -> clean
```

388 passing tests are 388 passing assertions against fake brokers and temporary
databases in one Linux container. They are not evidence about runtime behaviour
on Windows, against a real terminal, or under real market conditions — and no
count of them would be.
