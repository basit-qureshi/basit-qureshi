# Owner runbook

Repository root on your machine: `C:\Users\Home\Documents\basit-qureshi`

**State of the work:** these commits exist **only in the development
environment — they are LOCAL and have NOT been pushed.**
`origin/claude/forex-ai-trading-bot-izgn6l` is still at `1116af1`. A `git pull`
will therefore bring you **nothing new** until they are pushed. Say the word
and they go up.

Commits waiting: `b1ba98c` (Phase A) · `83b81f5` (Phase B) · `be9f84e`
(Phase C) · `e529600` (Phase D) · `e9f1e21` (Phase E) · plus Phase F.

---

## A. Safe offline — touches no broker, no network

### A1. Back up first

```powershell
cd C:\Users\Home\Documents\basit-qureshi
Copy-Item backend\.env                  backend\.env.bak                  -Force
Copy-Item backend\runtime_settings.json backend\runtime_settings.bak.json -Force
Copy-Item backend\trading_bot.db        backend\trading_bot.bak.db        -Force
Get-ChildItem backend\*.bak*, backend\*.bak.db | Select-Object Name, Length
```

### A2. Update (only after the commits are pushed)

```powershell
cd C:\Users\Home\Documents\basit-qureshi
git status
git config pull.ff only
git fetch origin claude/forex-ai-trading-bot-izgn6l
git log --oneline HEAD..origin/claude/forex-ai-trading-bot-izgn6l
```

If that last command prints nothing, there is nothing new to pull — stop here.
If it lists commits:

```powershell
git pull origin claude/forex-ai-trading-bot-izgn6l
git log --oneline -6
```

### A3. Dependencies and tests

```powershell
cd C:\Users\Home\Documents\basit-qureshi\backend
.\venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-mt5.txt
.\venv\Scripts\python.exe -m pytest -q
```
Expect **388 passed**. Anything else, stop and send the output.

```powershell
cd C:\Users\Home\Documents\basit-qureshi\frontend
npm ci
npm test
npm run lint
npm run build
```

### A4. Offline reports

```powershell
cd C:\Users\Home\Documents\basit-qureshi\backend
.\venv\Scripts\python.exe -m tests.bench.bench_execution
.\venv\Scripts\python.exe -m tests.bench.replay_policies
.\venv\Scripts\python.exe -m tests.bench.run_phase_c
.\venv\Scripts\python.exe -m tests.bench.collector_dryrun
```

The last one drives a whole recorded session against the **fake** broker —
grid placed, filled, flattened at the day's limit, settled, a revised figure
corrected, a link dropped and restored — then exports the packet and scans it.
It must end `rc 0`. It uses no terminal and no network. Its prices are a
fixture, so it proves the *recorder* works and says nothing about results.

### A5. Collect diagnostics (redacted by construction)

With the backend running, these read state only. **None of them prints a
password, login or server name** — `/api/status` exposes an account
*identifier*, so redact it before sharing if you would rather not.

```powershell
$out = "C:\Users\Home\Documents\basit-qureshi\diagnostics"
New-Item -ItemType Directory -Force -Path $out | Out-Null
Invoke-RestMethod http://127.0.0.1:8000/api/status          | ConvertTo-Json -Depth 6 | Set-Content $out\status.json
Invoke-RestMethod http://127.0.0.1:8000/api/open-trades     | ConvertTo-Json -Depth 6 | Set-Content $out\open-trades.json
Invoke-RestMethod http://127.0.0.1:8000/api/ai-status       | ConvertTo-Json -Depth 6 | Set-Content $out\ai-status.json
Invoke-RestMethod http://127.0.0.1:8000/api/research-profiles | ConvertTo-Json -Depth 6 | Set-Content $out\profiles.json
Invoke-RestMethod http://127.0.0.1:8000/api/trades?page=1`&page_size=50 | ConvertTo-Json -Depth 6 | Set-Content $out\trades.json
```

Before sending, check nothing sensitive leaked:

```powershell
Select-String -Path $out\*.json -Pattern 'password|login|MT5_|secret' -SimpleMatch
```

### A6. Restore a compatible state

```powershell
cd C:\Users\Home\Documents\basit-qureshi
Copy-Item backend\.env.bak                  backend\.env                  -Force
Copy-Item backend\runtime_settings.bak.json backend\runtime_settings.json -Force
Copy-Item backend\trading_bot.bak.db        backend\trading_bot.db        -Force
```

> **Rolling back code across a schema change is not automatically safe.**
> Phase A added `close_reason` to `trades` and a `risk_state` table. Older code
> does not write `close_reason` and knows nothing about `risk_state`, so a
> code-only rollback leaves new rows the current code reads as incomplete, and
> **an unresolved halt stored in `risk_state` would stop being enforced.**
> If you roll back code, restore `trading_bot.bak.db` with it.

---

### A7. Your own completed-grid number — offline, no terminal, no risk

```powershell
cd C:\Users\Home\Documents\basit-qureshi\backend
.\venv\Scripts\python.exe tools\grid_fit_report.py --assumptions
.\venv\Scripts\python.exe tools\grid_fit_report.py --alternatives ^
    --spread 0.25 --min-stop-distance 0.75 ^
    --basket-stop 60 --daily-loss 100 --balance 1000 --floor 800
```

Reads nothing, opens no terminal, cannot place an order. Every input is printed
back with its provenance, so a FIXTURE value is never mistaken for your
broker's. Read your live `spread` and `min stop distance` off the Grid panel or
`/api/status` while the backend is running, then pass them in.

## B. Requires the MT5 terminal — you run these, not the development environment

### B1. Export ticks (reads history, cannot place an order)

This is the **one thing worth doing next**, and it does not wait on any risk
decision: the exporter imports nothing from the app, so no engine and no order
path is reachable from it, and the only terminal calls it makes are
`initialize`, `symbol_select`, `copy_ticks_range` and `shutdown`.
`tests/test_export_ticks.py` asserts that offline against a fake MT5 module.

It DOES need the MT5 terminal open and logged in, because only the terminal
holds the history.

```powershell
cd C:\Users\Home\Documents\basit-qureshi\backend
New-Item -ItemType Directory -Force -Path data | Out-Null
.\venv\Scripts\python.exe tools\export_ticks.py --symbol XAUUSDm ^
    --from 2026-06-25 --to 2026-09-24 --out data\xauusdm_ticks.csv
```

If it stops part way (terminal restart, connection drop), continue it:

```powershell
.\venv\Scripts\python.exe tools\export_ticks.py --symbol XAUUSDm ^
    --from 2026-06-25 --to 2026-09-24 --out data\xauusdm_ticks.csv --resume
```

Real flags, as the file actually defines them: `--symbol`, `--from`, `--to`,
`--out` (all required), plus `--chunk-days` (default 1), `--resume`,
`--overwrite`, `--manifest`. There is no other flag.

**Dates are INCLUSIVE UTC days.** `--to 2026-09-24` covers up to
2026-09-24 23:59:59.999 UTC. Each day is a separate bounded request, so memory
stays flat and a partial run can be resumed.

**Read the manifest afterwards.** It lands at
`data\xauusdm_ticks.csv.manifest.json` and a successful run is not proof of
coverage:

```powershell
Get-Content data\xauusdm_ticks.csv.manifest.json | ConvertFrom-Json |
    Select-Object rows_total_in_file, rows_skipped_no_finite_two_sided_quote,
                  rows_out_of_order, days_with_no_rows_expected_closed,
                  coverage_is_complete
(Get-Content data\xauusdm_ticks.csv.manifest.json | ConvertFrom-Json).unexplained_gaps
```

`unexplained_gaps` lists weekdays that came back empty. Saturdays and Sundays
are classified as ordinary closures and are not flagged; a public holiday will
appear as an unexplained gap, which is the safer direction to be wrong in.
Nothing is ever interpolated — a gap is reported, never filled.

**What the file must contain:**

- `time_utc,bid,ask` (plus `last,volume,flags,seq`)
- **bid AND ask on every row** — a mid-only export cannot price a strategy that
  pays the spread on up to 20 fills against a fixed cash target. Rows without a
  finite two-sided quote are skipped and counted, never silently dropped
- the **exact symbol the bot trades, including the broker suffix**
- **tick resolution, not M1 bars** — bars cannot order intrabar events

**About the range.** Three months is a starting collection window, not a
statistically sufficient sample, and nothing here assumes your broker keeps that
much tick history. Export what exists, send the manifest with it, and the
coverage decides what can be measured. If the history is short, the answer is
prospective collection from now on, not a bigger claim from less data.

### B2. Start the backend (this does NOT start trading)

```powershell
cd C:\Users\Home\Documents\basit-qureshi\backend
.\venv\Scripts\python.exe run.py
```
Second window:
```powershell
cd C:\Users\Home\Documents\basit-qureshi\frontend
npm run dev
```

---

## C. Before stopping the backend for any update

The backend is **the only thing watching your positions.** There is **no
broker-hosted stop loss** — no SL or TP is attached to any order, so if this
process stops, nothing closes anything.

1. Press **Pause entries**. Existing positions stay managed.
2. Wait until the Open trades panel shows **0 positions AND 0 resting orders**
   — or press **Close positions** and wait for the same confirmation.
   "unknown" is not zero; if it says unknown, check the terminal.
3. Only then stop the backend.

Do not restart blind over open exposure.

---

## D. Controlled demo verification — PREPARED, NOT EXECUTED

Nothing below has been run. It needs the three risk numbers first (§E).

**D0 — prerequisites.** Funded demo account. `BROKER_MODE=mt5`, `ACCOUNT_TYPE=demo`,
`SYMBOL` matching Market Watch exactly, and the three risk numbers saved.

**D1 — identity.** `/api/status` → confirm `account_verified` matches the demo
account and `broker_trade_mode` reads `demo`. That field is what **the broker**
classified the account as; `mode` next to it is what the app is set to. They
are two different facts and the bot refuses to start when they disagree —
confirm that yourself by setting the app to `real` against the demo terminal
and watching it refuse.

**D2 — ownership scope.** Open **one manual trade by hand** in MT5 on a
different magic number. Confirm it never appears in the bot's Open trades panel
or its trade history, and that **Close positions** does not touch it.

**D3 — budgets visible.** Confirm the Grid panel shows your capital floor,
basket stop, daily limit and the marked daily reading, and that the numbers
match what you saved.

**D4 — pause behaviour.** With a grid resting, press **Pause entries**.
Expect: resting orders cancelled, any open positions still listed and still
managed, entries refused with a stated reason. Press **Resume entries** and
confirm a grid returns only after all gates pass.

**D5 — one supervised lifecycle.** Let exactly one basket run. Watch it reach
either its target or its stop. Confirm the close reason in the history table
matches what the panel said, and that the daily marked reading moved by the
right amount.

**D6 — truthful exposure.** Close the MT5 terminal for ~10 seconds while a
basket is open. Expect the **blocked** banner and "unknown" rather than a
confident zero. Reopen it and confirm the banner clears. *(This is the only
interruption in the procedure; it is brief, supervised, and on demo. All
destructive fault injection stays in the fake-broker harness.)*

**D7 — recovery.** With the basket **flat** (§C), stop and restart the backend.
Confirm the day's accounting, any halt and the drawdown anchor all survive.

**Stop the procedure immediately** if: exposure appears that the bot does not
own, a halt clears without you clearing it, the panel shows a confident zero
while MT5 shows positions, or the daily marked reading disagrees with the
account by more than the exit reserve.

Record the session while you do this — §F.

---

## E. Decisions needed before any forward evaluation can start

These are yours. They are not values to invent.

| Decision | Currently | Note |
| --- | --- | --- |
| `GRID_BASKET_STOP_LOSS_USD` | 0 | Must exceed the completed-grid estimate for your symbol and settings, or admission refuses with a stated reason. Account currency, not necessarily USD |
| `GRID_MAX_DAILY_LOSS_USD` | 100 | Judged on the **marked** daily result, floating loss included. What is LEFT of it must cover the same estimate — see below |
| `GRID_CAPITAL_FLOOR_USD` | **0 — blocks all new entries** | The balance below which no new grid is placed. Balance minus the estimate must stay above it |
| Broker-side SL | absent | The unresolved conflict: original spec asked for it, locked strategy forbids per-trade SL/TP |

> **All three numbers are measured against the same completed-grid estimate,
> and that estimate is NOT a fixed dollar figure.** It is computed at admission
> from your broker's own spread, minimum stop distance, point size and point
> value, together with your levels, lot and spacing. The **$37.80** quoted in
> earlier reports is what *this project's test fixture* produces (price 4000,
> spread 0.24, zero minimum stop distance, 1.00 per point per lot). Your
> terminal will produce a different number, and on MT5 the minimum stop
> distance is itself derived from the live spread, so a wider spread pushes
> every level further out:
>
> | spread | min stop distance | estimate |
> | --- | --- | --- |
> | 0.24 | 0.00 (fixture) | 37.80 |
> | 0.25 | 0.75 | 47.00 |
> | 0.30 | 0.90 | 51.00 |
> | 0.50 | 1.50 | 67.00 |
>
> Get your own number offline, with no terminal and no risk (§A7):
>
> ```powershell
> .\venv\Scripts\python.exe tools\grid_fit_report.py --assumptions ^
>     --spread 0.25 --min-stop-distance 0.75 --basket-stop 60 --daily-loss 100
> ```
>
> **What the estimate is and is not.** It is one scenario: every level filled,
> equal volume both sides, valued as if closed back at the reference price,
> entry spread paid once per fill. It is **not a maximum loss** — it excludes
> the exit spread, commission, swap and slippage, and it does not describe
> partial fills, unequal fills or one-directional exposure, which is not frozen
> and is bounded by the basket stop instead.
>
> **What the refusal means.** Admission refuses a grid whose estimate for that
> scenario exceeds the budget that would have to absorb it. That is a declared
> policy about what may be opened. It is **not** a prediction that the basket
> would have lost: a basket refused on those grounds might well have reached its
> target. If the estimate does not fit the limits you choose, the rejection
> stands — the answer is not to raise your tolerated loss or add money to clear
> a gate. A smaller grid profile is arithmetic you can inspect with
> `--alternatives`, untested and not active, for a decision you make later.
>
> **These changes improve prevention and recovery. They do not make a limit
> absolute.** A gap, a rejected close, a terminal that stops answering, or
> movement between two protective cycles can still overshoot a trigger.

### Forward evaluation plan — fixed before any results arrive

- **Frozen profile:** `baseline@v1`, unchanged. No research profile is live.
- **Costs:** your broker's actual commission and swap, recorded before the run
  and not adjusted afterwards.
- **Eligible sessions:** whatever `GRID_TRADING_START_HOUR`/`END_HOUR` you set,
  fixed in advance.
- **Minimum observations:** decided by the uncertainty you need, not by a
  calendar. With losses that can exceed wins, a handful of baskets cannot
  separate skill from variance. **State the effect size worth detecting and the
  count follows from it** — I am not going to name a number of days and call it
  proof.
- **Data quality:** the run is void if the terminal was disconnected for a
  material part of it, or if the exposure panel showed "unknown" during a
  breach.
- **Stop conditions:** stop if the capital floor is reached, if a halt does not
  clear correctly, if exposure appears that the bot does not own, or if the
  panel and MT5 disagree.
- **Separation:** AI shadow observations and replay outcomes are **simulated**
  and never counted with executed demo trades. Demo results are **not** live
  evidence. There is no automatic promotion to real trading, no automatic
  retraining, and no risk increase after a loss.

---

## F. Recording a session and exporting the evidence

A **recording** is not a trading session. Starting one writes files; it places
no order and starts nothing. Stopping one stops writing files; it closes
nothing and does not stop the bot. Nothing is recorded unless you start it.

### F1. Pre-flight — run every line before connecting anything

```powershell
$api = "http://127.0.0.1:8000/api"
$s = Invoke-RestMethod $api/status
$s | Select-Object mode, broker_trade_mode, account_verified, symbol, halted,
                   entries_paused, entry_block_reason, capital_floor_usd,
                   basket_stop_loss_usd, max_daily_loss_usd, connected
$s.day_risk
Invoke-RestMethod $api/open-trades | Select-Object connected, pending_orders_known,
                   pending_orders, @{n='positions';e={$_.positions.Count}}
Invoke-RestMethod $api/ai-status   | Select-Object mode, model_loaded
```

Check, and do not start if any line fails:

| Check | Required |
| --- | --- |
| Account identity | `account_verified` is the **demo** account you intend |
| Broker classification | `broker_trade_mode` = `demo`, and `mode` = `demo` |
| Symbol | `symbol` matches Market Watch **exactly**, suffix included |
| Ownership scope | `/api/open-trades` shows only orders with your magic number; anything you opened by hand is absent |
| Current exposure | you know what is open **before** you start, or you start flat |
| Risk budgets | `capital_floor_usd`, `basket_stop_loss_usd`, `max_daily_loss_usd` are your saved numbers, not zeros (§E) |
| Basket stop sanity | it exceeds the completed-grid estimate for YOUR symbol (§A7 prints it), or admission refuses every grid and says so |
| Margin | `day_risk` is complete and `entry_block_reason` is not a margin or unknown block |
| Halt state | `halted` is `null`. A halt that survived a restart is **not** noise — find out what caused it first |
| AI | `mode` = `disabled`. A model has never been trained; there is nothing to enable |

### F2. Start the recording

```powershell
Invoke-RestMethod -Method Post $api/session/start | ConvertTo-Json -Depth 5
```

This writes `backend\evidence\<session-id>.manifest.json` and
`.events.jsonl`. It **starts no trading.** Note the `session_id`. A second
start refuses rather than splitting one run across two files; pass
`?replace=true` only if you mean to abandon the first.

Confirm the manifest says what you expect — particularly
`account_type: verified_demo`. If it says `unverified`, the broker has not been
asked yet or did not answer, and the session will be labelled that way
permanently. That is the field doing its job, not a bug to work around.

### F3. During the session

Watch the dashboard. Stop the run — press **Pause entries**, then follow §C —
if any of these happen:

- exposure appears that the bot does not own, or a position changes owner
- the account identity changes mid-session
- the panel shows a confident **zero** while MT5 shows positions
- a halt clears without you clearing it, or does not fire when a budget breaks
- the daily marked reading disagrees with the account by more than the exit reserve
- an owner limit is breached, or overshot by more than the exit reserve
- `/api/session/status` reports `dropped_events > 0` or `storage_errors` —
  the record is no longer complete, so the session no longer evidences anything

### F4. End the session in this order

1. **Pause entries.** Existing positions stay managed.
2. Wait for **0 positions AND 0 resting orders**, or press **Close positions**
   and wait for the same confirmation. "unknown" is not zero.
3. Stop the recording:
   ```powershell
   Invoke-RestMethod -Method Post $api/session/stop | ConvertTo-Json -Depth 5
   ```
4. Only then stop the backend, per §C.

**Never kill the backend to end a recording while anything is open.** It is
the only thing watching those positions — there is no broker-hosted stop.
Stopping the recording is step 3 precisely so it is never a reason to skip
step 2.

### F5. Export the redacted packet

```powershell
cd C:\Users\Home\Documents\basit-qureshi\backend
.\venv\Scripts\python.exe tools\export_session.py --list
.\venv\Scripts\python.exe tools\export_session.py --session <session-id> `
    --out C:\Users\Home\Documents\basit-qureshi\diagnostics\session.packet.json
```

The tool removes passwords, logins, server names, tokens and local database
paths, replaces the account number with a stable `acct_…` reference, then scans
the finished file and **refuses to write it** if anything private survived,
naming the events to look at. If it refuses: read those events, and fix what
logged that text. **Do not edit the event log to make it pass** — the log is
the evidence.

Check it yourself before sending. Both of these should print nothing:

```powershell
Select-String -Path ...\diagnostics\session.packet.json `
    -Pattern 'password|mt5_login|MT5_SERVER|Exness-' -SimpleMatch
Select-String -Path ...\diagnostics\session.packet.json -Pattern 'C:\\Users' -SimpleMatch
```

Send the **packet**. Never send the `evidence\` folder — it is unredacted by
design, and it is what the packet is made from.

### F6. What a session packet can and cannot settle

It can settle: what the bot decided and why, what was open at each point, that
a close was *confirmed* rather than merely sent, how long protective cycles
took, where coverage was lost, and whether a figure was revised afterwards.

It cannot settle whether the strategy makes money. A demo run is a handful of
baskets on one stretch of market; with losses that can exceed wins, that
separates nothing from variance. Read `close_intents_unconfirmed`,
`coverage_gaps` and `corrections` first — a session with gaps in the wrong
places evidences less than it looks like it does.

---
