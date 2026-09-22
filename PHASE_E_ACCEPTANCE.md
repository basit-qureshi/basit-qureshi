# Phase E acceptance packet

Source revision: `e529600` + this phase's fixes. Branch
`claude/forex-ai-trading-bot-izgn6l`.
**Commits are LOCAL. Nothing has been pushed.** `origin` is at `1116af1`.

## The four separate verdicts

| Question | Answer |
| --- | --- |
| Are the implemented **offline mechanics** verified? | **Yes**, within the stated boundary: 283 backend tests against injected fake brokers and temporary databases. |
| Is **controlled demo verification** ready? | **Yes — prepared, not executed.** See `OWNER_RUNBOOK.md`. It needs three owner risk numbers first. |
| Has any candidate shown **credible net improvement**? | **No.** There is no market data, so no performance evidence exists at all. |
| Is **real trading activation** authorized? | **No.** Not by this phase, and not by any previous one. |

These are deliberately not combined into one "production ready" label, because
three of the four are not the same answer.

---

## 1. Acceptance ledger

Legend: **IV** implemented and verified · **IU** implemented but unverified ·
**M** missing · **F** failed · **B** blocked by a named dependency.

| # | Requirement | Where | Evidence | Limitation | Status |
|---|---|---|---|---|---|
| 1 | Broker account identity verified before every entry path | `grid_engine.verify_account`, `routes._require_verified_account` | `test_a_real_account_behind_a_demo_label_is_refused_on_every_path`; API 403 on `/start` and `/test-order` | Fake broker only; no real MT5 `account_info()` observed | **IV** (offline) |
| 2 | Entry admission refuses on unknowns | `grid_engine._entry_gate` | 12 Phase A/E tests incl. unknown margin, uncalculable margin, missing anchor, persistence failure | — | **IV** |
| 3 | **Admission refuses while halted** | `_entry_gate` (**fixed this phase**) | `test_a_settings_rebuild_does_not_erase_a_breached_budget` | — | **IV** |
| 4 | Complete grid placement, geometry unchanged | `_build_grid` | `test_grid_shape_lot_and_spacing`, `test_original_grid_has_fixed_lots_spacing_and_no_new_entry_filters` | — | **IV** |
| 5 | Partial placement leaves a visible error, not a silent partial | `_build_grid` + `_last_error` | `test_a_rejected_placement_leaves_a_visible_error_not_a_silent_partial` | — | **IV** |
| 6 | A partial grid is not topped up | `_tick` rebuild guard | `test_a_partial_grid_is_not_topped_up_on_the_next_cycle` | — | **IV** |
| 7 | Position ownership by magic number, manual trades untouched | every broker call passes `magic=` | `test_no_path_touches_another_magic_number` (manual + second EA) | — | **IV** |
| 8 | Basket valuation net of swap/commission, exit reserve separate | `_basket_pnl`, `_estimated_exit_cost` | `test_basket_target_waits_for_net_profit_not_gross`, `test_an_unestimable_exit_cost_is_none_not_zero` | Whether MT5's floating profit already includes the exit spread is **unverified** | **IU** |
| 9 | Profit exit; unknown costs cannot claim the target | `_profit_target_met` | `test_unknown_costs_do_not_qualify...`, `test_missing_symbol_info_does_not_let_a_basket_claim_the_target` | — | **IV** |
| 10 | Loss exit durable, survives price recovery and restart | `CloseIntent`, `_drive_close_intent` | `test_a_basket_stop_keeps_closing_after_the_price_recovers`, `test_a_rebuilt_engine_resumes_an_unfinished_close`, subprocess test | — | **IV** |
| 11 | Daily protection includes floating exposure | `risk_accounting`, `_check_risk_limits` | `test_floating_loss_alone_can_reach_the_daily_limit` + the 3 owner invariants | — | **IV** |
| 12 | Overall drawdown anchor persists across days | `_roll_day` | `test_the_drawdown_anchor_is_not_reset_by_a_new_day` | — | **IV** |
| 13 | Pause keeps protecting; Stop reports what is open | `pause_entries`, `stop` | `test_pause_entries_keeps_protecting...`, `test_stop_reports_what_is_still_open...` | — | **IV** |
| 14 | Restart recovery of halt, intent, day anchor | `_restore_risk_state` | `test_state_survives_a_real_process_restart` (**separate process**) | — | **IV** |
| 15 | Same-candle replacement requires confirmed flat + all gates | `_retire_finished_intent`, `_consider_entry` | `test_same_candle_replacement_requires_a_confirmed_flat_basket`, `test_replacement_still_passes_every_admission_gate` | — | **IV** |
| 16 | UI snapshots: unknown ≠ confirmed zero; ordering | `_broadcast`, `App.jsx` `acceptSnapshot` | `test_a_failed_position_read_is_not_confirmed_zero_exposure`, `test_a_failed_pending_read_is_unknown_not_zero` | Frontend suite is 4 tests; the panels are not rendered under test | **IU** |
| 17 | History settlement idempotent; close reason not overwritten | `_settle_closed_trades` | `test_settling_the_same_ticket_twice_does_not_double_count`, `test_a_repeated_close_reason_does_not_overwrite_the_original` | — | **IV** |
| 18 | Only one broker writer | `BrokerOwner`, single `create_task` | `test_a_stalled_broker_call_reports_blocked_and_never_duplicates`; grep confirms one task, no threads | Priority is untested under real concurrency with the REST threadpool | **IU** |
| 19 | Migration preserves ownership, reasons, fees, timestamps | `db.init_db`, `_ADDED_COLUMNS` | `test_migration_preserves_...`, `test_migration_is_idempotent`, `test_a_backup_restores_...` | Disposable copies, not the owner's real database | **IV** (offline) |
| 20 | AI shadow cannot mutate orders or configuration | not wired into the engine at all | `test_ai_shadow_cannot_mutate_orders_or_configuration` greps the engine source | — | **IV** |
| 21 | Missing model does not block protective exits | — | `test_a_missing_model_does_not_block_a_protective_exit` | — | **IV** |
| 22 | Runtime knobs documented with units/scope/defaults | `.env.example` (**fixed this phase**) | `test_the_cadence_knobs_reach_the_engine` | — | **IV** |
| 23 | **Broker-hosted protective orders (SL at the broker)** | — | — | Unresolved owner decision since Phase A | **M** |
| 24 | Performance evidence for any profile | — | — | No tick data exists | **B** — blocked by missing market data |
| 25 | Trained entry model | `tools/train_entry_model.py` | Refuses without real ticks (exit 2) | Same dependency | **B** — blocked by missing market data |
| 26 | Owner risk numbers (basket stop / daily loss / capital floor) | settings | `GRID_CAPITAL_FLOOR_USD=0` blocks entries by design | Owner decision, not a value to invent | **B** — blocked by owner input |

---

## 2. Defects found and fixed this phase

| Defect | Why it mattered | Fix | Regression |
| --- | --- | --- | --- |
| **`_entry_gate` did not check `_halt_reason`** — the function that answers "may a new basket be created" returned **allowed** during an active halt. The live loop happened to be safe because `_reporting_tick` returns early while halted, so the gate was correct only because something else checked first. Any other caller — a research profile, the AI admission path, a UI preview — would have been told the wrong thing. | A halt is the strongest refusal in the system and the admission function ignored it | `_entry_gate` now returns `HALTED: <reason>` first | `test_a_settings_rebuild_does_not_erase_a_breached_budget` |
| **`POLL_INTERVAL_SECONDS` was a dead knob.** Still in settings, still passed to the engine, still editable — and since Phase B it drives nothing. An owner could change it, see no effect, and be unable to tell whether the bot was broken. | A silently-ignored control is worse than a removed one | `status()` reports `poll_interval_seconds_retired: true`; `.env.example` says RETIRED in both places it appears | `test_poll_interval_seconds_is_declared_retired_not_silently_ignored` |
| **Three real cadence knobs were undocumented** (`PROTECTIVE_POLL_SECONDS`, `REPORTING_POLL_SECONDS`, `BROKER_STALL_AFTER_MS`) — no units, defaults or scope anywhere the owner would look | §3 requires every runtime knob to be documented | Added to `.env.example` with units, rationale and the measurement behind the default | `test_the_cadence_knobs_reach_the_engine` |

Audited and found **clean**: no stubs on the traced paths, one asyncio task and
no threads (single writer), no orphaned config fields besides the retired knob
above, and no test that bypasses the production path — `_tick` composes
`_protective_tick` + `_reporting_tick`, which is exactly what the loop runs.

---

## 3. Verification actually performed

Environment: Linux container, Python 3.11, numpy 2.4.6, pandas 3.0.6.
**scikit-learn, scipy and joblib are absent.** No MT5, no terminal, no network
provider, no credentials read.

```
cd backend && python3 -m pytest -q                    -> 283 passed
cd backend && python3 -m compileall -q app tools      -> OK
cd frontend && npm test                               -> 4 passed
cd frontend && npm run lint                           -> 0 errors, 4 warnings (pre-existing)
cd frontend && npm run build                          -> clean
cd backend && python3 -m tests.bench.bench_execution  -> timing
cd backend && python3 -m tests.bench.replay_policies  -> policy comparison
cd backend && python3 -m tests.bench.run_phase_c      -> BLOCKED BY DATA
cd backend && python3 tools/train_entry_model.py --ticks data/absent.csv --out /tmp/m.json
                                                      -> exit 2, refuses
```

**Evidence classes, kept separate:**

| Class | Present |
| --- | --- |
| Synthetic contract tests | **yes** — 283 backend, 4 frontend |
| Historical replay on real data | **no** — no dataset |
| Windows terminal checks | **no** |
| Observed broker behaviour | **no** |

Dataset hashes: none, because there is no dataset. The manifest records
`dataset.present = false` rather than omitting the field.

### Faults injected (all through the fake broker)

Broker read failure · stale/unavailable symbol info · missing costs · rejected
placement · partial placement · delayed acknowledgement · fill during
cancellation · account switch · persistence write failure · process restart
(separate interpreter) · midnight transition · slow reporting · failing
websocket consumer · missing AI model.

---

## 4. What the performance evidence proves

**Nothing. There is no performance evidence.**

| Profile | Net | Drawdown | Baskets | Sessions | Verdict |
| --- | --- | --- | --- | --- | --- |
| `baseline@v1` | — | — | — | — | no data |
| Phase C candidates (5) | — | — | — | — | no data |
| Phase D model | — | — | — | — | no model, no data |

The only run that exists is the Phase C **synthetic** harness, and its own
table shows why it settles nothing: the baseline completed **0** baskets and
ended with one open at **−$24.58**; `research-regime` showed `+10.05` alongside
an open basket at **−$33.46**, so it is not ahead. **One basket is one
observation.**

What *is* established, and it comes from the code rather than from data: this
strategy is a **symmetric breakout straddle** needing ~2.9 price units of
one-directional movement to clear a $10 target, and it **freezes near −$37.80**
when price oscillates enough to fill both sides and returns. The synthetic run
reproduced that freeze — the baseline filled 18 of 20 levels, peaked at
**+$5.63** without ever reaching the target, and stuck.

**Decision: no candidate accepted.** Not rejected either — untested.

**The next research question, unchanged since Phase C:** over real history, how
often does a basket reach its target versus reach the frozen state? If freezing
dominates, no entry filter and no model fixes it, because the geometry is the
problem and a model would simply learn to predict losses accurately.

---

## 5. Unresolved blockers

1. **No market data.** Blocks every performance claim, Phase C selection and
   Phase D training. Smallest sufficient request is in `OWNER_RUNBOOK.md`.
2. **No broker-side stop loss.** All protection runs in the Python process; if
   it is killed, nothing in this bot protects the account. A loss threshold is
   a trigger, not a guaranteed fill through a gap. Unresolved owner decision
   since Phase A — the original specification asked for individual SL, the
   locked strategy forbids per-trade SL/TP.
3. **Three owner risk numbers unset.** `GRID_CAPITAL_FLOOR_USD = 0` blocks new
   entries by design. These are decisions, not values to invent.
4. **No Windows/broker verification of anything.**

---

## 6. Next step

Review this packet. Then, in order: export ticks → measure the target-versus-
freeze ratio → decide whether the geometry is viable at all. Demo verification
can proceed in parallel once the three risk numbers exist, but demo verifies
*operation*, not edge.

The next step is **not** another development phase.
