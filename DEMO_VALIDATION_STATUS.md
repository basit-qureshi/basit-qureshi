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
| Do the **offline checks** pass? | **Yes.** 477 backend tests, 4 frontend tests, lint clean, build clean, and a fixture dry-run that records a full session and exports it. Offline, against fake brokers — see §7.8 for what that does and does not establish. |
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

---

## 8. Focused review round — five corrections

### 8.1 Broker stop distance is not the same thing as this app's buffer

`min_stop_distance` was one number that silently merged three, one of them a
broker rule and two of them ours. They are now carried separately through
`SymbolInfo`, `grid_math.SymbolSpec` and `/api/status`, with the effective value
— and therefore actual placement — **unchanged**:

| Term | Whose | Value on MT5 |
| --- | --- | --- |
| `broker_stop_level_distance` | **the broker's** | `trade_stops_level × point` |
| `app_stop_buffer` | **ours** (`APP_STOP_BUFFER_POINTS = 5`) | 5 points, only when the broker declares a minimum |
| `app_spread_multiple_distance` | **ours** (`APP_SPREAD_MULTIPLE = 3.0`) | `spread × 3`. No broker states this rule |

Effective distance is `max(broker + buffer, spread × 3)`, exactly as before; the
first grid level goes at `max(grid distance, effective)`.

**What this exposed.** On gold at the fixture's own 0.24 spread, the *app's*
spread multiple gives 0.72 — wider than the 0.30 grid distance — so an
application choice, not a broker requirement, moves every level:

| Effective first step | Set by | Estimate |
| --- | --- | --- |
| 0.30 | the grid distance (the test double declares no minimum at all) | **37.80** |
| 0.72 | **this app's** spread × 3, at spread 0.24 | **46.20** |
| 1.05 | a broker declaring 100 points, **plus this app's 5** | **52.80** |

So the `37.80` quoted throughout earlier reports is not merely fixture-specific —
it is lower than what the real adapter would produce on the *same spread*,
because the test double bypasses the app's own heuristic. Pinned by
`test_the_app_heuristic_not_the_broker_is_what_binds_on_gold`. The live split is
readable at `/api/status` → `stop_distance`, alongside
`completed_grid_estimate`.

Placement is untouched: no setting changed, and the full suite still passes.

### 8.2 Admission and excluded closing costs

**The claim that is now corrected.** The report used to print `FITS` against a
budget. That was an unsupported claim of budget compatibility: admission compares
the **entry-side estimate** against a budget and nothing more, and the
remaining-daily figure it compares against (`marked_result`) excludes the exit
reserve as well. **Neither side of the comparison carries a closing-cost
buffer.**

The verdict now reads "entry-side estimate fits, X left over", followed by
whether X covers closing costs — and if any component is missing it says the
answer is **NOT established**, by the report or by admission.

Closing costs are taken only from owner-supplied figures
(`--commission-per-lot-per-side`, `--exit-spread`,
`--slippage-points-per-fill`). Anything not supplied prints **UNKNOWN**, never
zero, and no total is claimed while any component is unknown. **Swap is always
UNKNOWN** — it depends on nights held and this tool does not model it. No fee is
invented, no limit is raised, and no unknown is treated as zero. Pinned by
`test_the_report_refuses_to_total_unknown_closing_costs` and
`test_supplied_closing_costs_are_used_and_compared`.

The engine is unchanged here by design: adding a closing-cost buffer to
admission would require a fee figure nobody has supplied. The gap is reported
rather than papered over. `_estimated_exit_cost()` already returns `None` rather
than zero when costs are unknown, and the profit path already refuses to claim a
target on an unknown cost.

### 8.3 The three closure causes, and the late-exposure policy

The late-exposure test was rewritten once because it conflated causes. They are
now separated, named in the test file, and each has coverage:

| Cause | Latches entries? | Halt? | Late owned exposure |
| --- | --- | --- | --- |
| **Profit close** | no | no | **managed** — marked, valued, closed when a limit fires; not closed on sight. No new grid while it is open |
| **Basket stop close** | **yes** | no | **managed** under the same limits, entries stay paused |
| **Loss halt** (daily loss / drawdown) | **yes** | **yes** | **closed by the protective policy, immediately**, and entries stay paused while that happens |

New regressions in `tests/test_close_lifecycle.py` (15 tests total):

- `test_a_profit_close_retires_its_intent_without_latching` — counted as won,
  entries not paused, admission willing again
- `test_a_basket_stop_close_retires_its_intent_and_does_latch` — counted as
  stopped, entries paused, **not** a halt, no same-candle replacement
- `test_late_exposure_after_a_profit_close_is_managed_not_closed_on_sight` —
  managed, no grid placed on top of it, closed once it passes the basket stop
- `test_late_exposure_after_a_loss_halt_is_closed_while_entries_stay_paused` —
  closed immediately, halt still set, entries still paused, gate still `HALTED`,
  no grid placed
- `test_a_halt_closes_late_exposure_repeatedly_not_only_once` — the re-open
  branch is not one-shot
- `test_a_halt_does_not_close_a_manual_trade_it_does_not_own` — ownership by
  magic number holds through all of it

### 8.4 Windows export instructions corrected

The previous revision used `^` for line continuation inside PowerShell blocks.
`^` is cmd.exe's continuation character and does nothing useful in PowerShell, so
those commands would have failed as written. Every command in `OWNER_RUNBOOK.md`
is now a **single line**; there are zero `^` continuations left in the file.

The export command was then verified offline, as the exact argv, against a fake
MT5 module over a Thursday–Monday range: five inclusive days, five bounded
requests, 15 rows, weekend classified as an ordinary closure,
`unexplained_gaps` empty, manifest at `data\xauusdm_ticks.csv.manifest.json`.
Regression: `test_the_documented_single_line_command_works_end_to_end`.

Also fixed: after `--resume` the manifest recorded the *resumed* start as the
requested range. It now carries `requested_range_utc` and
`covered_this_run_utc` (with `resumed: true/false`) as separate facts.
Regression: `test_the_manifest_separates_what_was_requested_from_what_this_run_covered`.

No terminal was contacted for any of this.

### 8.5 Verification for this round

```
backend  python3 -m pytest -q                     -> 404 passed
backend  python3 -m compileall -q app tools       -> OK
backend  python3 -m tests.bench.collector_dryrun  -> rc 0
frontend npm test                                 -> 4 passed
```

404 passing tests are assertions against fake brokers and temporary databases in
one Linux container. No terminal, no order, no session, and trading performance
is still not measured.

---

## 9. Independent review round — six findings corrected

An independent review of `goldgrid_review_8319176.zip` applied the 12 patches to
verified base blobs, ran the suite (404 passed in its own Python 3.12 environment)
and added six offline probes. All six reproduced. Each is corrected below, with
the reproduction before and after.

Two of the six were **policy** disagreements rather than implementation slips, and
those are marked: the code did what the previous report said it did, and what it
said was not what the owner asked for.

### 9.1 Capital floor — a missing protection (implementation gap)

**Before:** balance 1000, floor 950, basket stop 100, daily limit 200. Admission
passed; a bot position took equity to 940; a protective cycle left it open with no
halt. The floor participated in admission and in nothing else, while the
constructor described it as a line the account is never traded below.

**After:** the floor is an active trigger as well as an entry rule, judged on
**account equity**, and it persists the reason before liquidating.

The contract, now explicit in code, `.env.example` and the halt reason:

| Question | Answer |
| --- | --- |
| Judged on | **account equity** — balance plus every floating position on the account |
| What it closes | **only what this bot owns**, by magic number. A manual trade or another strategy's position is never touched |
| Outside activity | equity includes trades this bot does not own, so an outside loss **can** trigger it. Flattening this bot cannot repair that: it removes its own contribution, latches entries, and the halt reason says so |
| With nothing owned | entry refusal only, no halt — a small account is not locked behind a manual reset for a breach its trading never caused |
| Guarantee | **none.** A gap, a rejected close or a terminal that stops answering can leave the account below the floor anyway |
| Costs | the incremental closing cost is included when it is **known** (§9.4); never invented |

Nine regressions, including persistence before liquidation, retry after rejection,
restart mid-liquidation, a late fill afterwards, a manual trade untouched, an
outside-loss trigger, and an unreadable equity.

**Found while writing those tests:** `_check_risk_limits` computed
`(peak − account.equity)` with no check that equity was a number, so a broker
returning `None` raised a `TypeError` **out of the protective cycle** — the one
place that must not raise. Equity readability is now established once, before any
arithmetic.

### 9.2 Late exposure after a loss stop (policy change, now implemented)

**Before:** after a basket stop, a late owned fill was marked and managed but not
liquidated, and a test explicitly required it to stay open until another threshold
fired. That was the engine's behaviour and the previous report defended it. It is
not "close and stop after the loss limit".

**After:** a confirmed close ends the close *attempt*; a loss stop leaves a
durable **liquidation policy** behind (`app/engine/lifecycle.py`). While it
stands, exposure this bot owns is cancelled and closed again. It is persisted,
survives a restart, and is cleared **only** by an explicit owner resume — which
itself refuses while owned exposure is still open.

The four states stay distinct:

| | Latches entries | Liquidation policy | Late owned exposure |
| --- | --- | --- | --- |
| Ordinary pause | yes | **no** | managed, never closed on sight |
| Profit close | no | **no** | managed; a replacement is permitted |
| Basket stop | yes | **yes** | cancelled and closed again |
| Risk halt (daily / drawdown / floor) | yes | **yes** | cancelled and closed again |

Cleanups carry `counts_basket=False`, so three late fills do not become three
stopped baskets. Fifteen regressions cover repeated fills, rejection and recovery,
a surviving resting order, restart, a price recovery that changes nothing, and a
manual trade left alone.

### 9.3 Fabricated symbol valuation (implementation gap)

**Before:** `MT5Broker.get_symbol_info` read `info.trade_tick_value or 1.0`, so a
broker reporting no tick value produced "one unit per point" and every figure
downstream inherited it. Reproduced against a fake terminal: raw tick value 0.0
became `pip_value_per_lot = 1.0`, and admission priced a grid on it.

**After:** validation, not defaulting. `SymbolInfo` carries `valuation_ok`,
`valuation_problems`, `quote_available` and `spread_available`; the estimate
carries `valid` and `problem`; admission refuses with the specific diagnostic and
keeps managing existing exposure. Covered: `0`, `None`, `NaN`, `inf`, negative,
non-numeric, crossed bid/ask, and a missing quote. **A genuinely observed zero
spread is usable; no quote at all is not** — the two no longer collapse into the
same number. A missing tick *size* still falls back to the point size, which is
the same quantity; a missing tick *value* falls back to nothing, because it is
the money.

### 9.4 Closing costs — one contract, unknowns that block

**Before:** `_estimated_exit_cost` returned an unverified half-spread; the daily
loss trigger ignored it; admission ignored the exit entirely; the fit report
printed a fourth arithmetic. Reproduced: marked −99.00 with a 2.00 reserve against
a 100.00 limit gave `risk_reading` −101.00, and the trigger read −99.00 and did
not fire.

**After:** `app/engine/costs.py` is the single contract, and it separates the four
quantities that behave differently:

| Quantity | Treatment |
| --- | --- |
| Already inside the broker's figure | `profit_includes_exit_spread` — `True` means the exit spread is **not** charged again; `None` (unverified) is an **unknown**, not an assumption either way |
| Already booked (swap, commission taken) | inside `net_profit`; **never added again** |
| Future commission (closing side) | owner's contract figure, or **UNKNOWN** |
| Slippage | owner's observed figure, or **UNKNOWN** |

Two readings, because the directions are not symmetric:

* **conservative** — may use an unverified upper bound, because it only ever
  *delays* a profit exit. This preserves the previous behaviour of the profit
  target.
* **defensible** — `None` unless every component is verified or owner-supplied.
  Only this one may bring a loss exit forward, and only this one satisfies
  admission.

The daily trigger now reads `risk_reading`, which equals the marked result exactly
when no defensible cost exists — so a **known** cost fires the limit earlier, and
an unverified one still cannot. Admission blocks when the prospective grid's
closing cost is unknown, naming the settings to supply. It prices the grid it
would **create**, not the empty book — the first version of that gate asked what
it costs to close nothing and passed trivially.

Three new settings, all defaulting to UNKNOWN rather than zero:
`EXIT_COMMISSION_PER_LOT_USD`, `SLIPPAGE_POINTS_PER_FILL`,
`BROKER_PROFIT_INCLUDES_EXIT_SPREAD`. **The owner's saved risk settings were not
changed.** Test doubles state their own truthful values (both charge nothing and
neither includes the exit spread in its profit figure).

### 9.5 Scheduling — the cadence, measured properly

**Before:** `_loop` awaited `_maybe_report`, which ran reporting synchronously.
Deterministic probe, 10 ms configured cadence and 250 ms of reporting work:
successive protective **starts** were 250 ms apart. A 25.9 ms benchmark of the
protective *function* said nothing about it.

**After**, same probe:

```
before: gaps 251.7, 250.7, 250.8, 250.6 ms     (every cycle)
after : gaps 264.0, 10.6, 10.7, 10.7, 10.5, 10.5, 17.5, 10.7, ... ms
        median 10.6 ms, worst 264.0 ms, 1 overrun recorded
```

Three mechanisms: reporting is **staged** against a time budget and defers the
history sweep and then the entry decision at stage boundaries; reporting's broker
reads go through the **single owner** at REPORTING priority so it can stand aside
between calls; and a cycle that still overruns puts reporting in an **absolute
cooldown** of four times what it consumed.

The first attempt at that cooldown multiplied the configured interval — and an
interval of zero stays zero however often it is doubled. A test now pins that
case.

**What is still true and is asserted rather than hidden:** a synchronous broker
call already in flight cannot be interrupted, so one cycle still absorbs it. The
worst case above (264 ms) is exactly that. `status().scheduling` reports
`cadence_delay_ms`, `protective_decision_ms`, `reporting_cycle_ms` and
`close_to_confirmed_flat_ms` separately, each labelled as an in-process span, with
a note that broker acknowledgement and fill times are **not** measured here. The
close-to-flat span is not fabricated across a restart, because the monotonic clock
that started it is gone.

### 9.6 Rounding at a decision boundary (implementation gap)

**Before:** `_basket_pnl` rounded to cents and its only live caller passed that to
`_profit_target_met`, which documented itself as comparing unrounded values.
Reproduced: net 9.996 against a 10.00 target — the direct helper answered `False`,
the real composition answered `True`.

**After:** `_basket_pnl` returns raw values, `_basket_pnl_display` rounds, and the
API card and broadcast use the display variant. Position marks are kept raw too.
Regressions go through the real composition, for the profit target and the basket
stop, in both directions.

### 9.7 Verification for this round

```
backend  python3 -m pytest -q                -> 477 passed
backend  python3 -m compileall -q app tools  -> OK
```

Six probes re-run after the fixes: the floor liquidates and latches; a late fill
after a basket stop is cleaned up; a zero tick value is refused instead of becoming
1.0; a known closing cost brings the daily limit forward; the protective cadence
recovers to its configured interval; and 9.996 no longer clears a 10.00 target.

**What none of this establishes.** No terminal was contacted, no order was placed,
no market data exists, and no session has been recorded. The figures 37.80, 46.20
and 52.80 remain conditional arithmetic for one scenario, not maximum losses.
**Profitability is still not measured**, and 477 passing offline tests are not
evidence of a trading edge.
