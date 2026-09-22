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
Expect **336 passed**. Anything else, stop and send the output.

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

## B. Requires the MT5 terminal — you run these, not the development environment

### B1. Export ticks (reads history, places no orders)

```powershell
cd C:\Users\Home\Documents\basit-qureshi\backend
.\venv\Scripts\python.exe tools\export_ticks.py --symbol XAUUSDm `
    --from 2026-06-01 --to 2026-09-20 --out data\xauusdm_ticks.csv
```

**Smallest sufficient request** — this one file unblocks Phase C selection and
Phase D training:

- columns `time_utc,bid,ask` (optional `last,volume,flags`)
- **bid AND ask on every row** — a mid-only export cannot price a strategy that
  pays the spread on up to 20 fills against a fixed cash target
- the **exact symbol the bot trades, including the broker suffix**
- **tick resolution, not M1 bars** — bars cannot order intrabar events
- **at least 3 months continuous**

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
| Basket stop sanity | it exceeds ≈ $37.80, or admission will refuse every grid and say so |
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

## E. Decisions needed before any forward evaluation can start

These are yours. They are not values to invent.

| Decision | Currently | Note |
| --- | --- | --- |
| `GRID_BASKET_STOP_LOSS_USD` | 0 | Must exceed the completed-grid scenario (≈ $37.80 at 10+10 / 0.01 / 0.30) or admission refuses with a stated reason |
| `GRID_MAX_DAILY_LOSS_USD` | 100 | Judged on the **marked** daily result, floating loss included |
| `GRID_CAPITAL_FLOOR_USD` | **0 — blocks all new entries** | The balance the account must never be traded down past |
| Broker-side SL | absent | The unresolved conflict: original spec asked for it, locked strategy forbids per-trade SL/TP |

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
