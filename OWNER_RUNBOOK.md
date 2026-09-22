# Owner runbook

Repository root on your machine: `C:\Users\Home\Documents\basit-qureshi`

**State of the work:** four commits exist **only on your machine's clone after
you pull — they are currently LOCAL in the development environment and have
NOT been pushed.** `origin/claude/forex-ai-trading-bot-izgn6l` is still at
`1116af1`. A `git pull` will therefore bring you **nothing new** until the
commits are pushed. Say the word and they go up.

Commits waiting: `b1ba98c` (Phase A) · `83b81f5` (Phase B) · `be9f84e`
(Phase C) · `e529600` (Phase D) · plus Phase E fixes.

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
Expect **283 passed**. Anything else, stop and send the output.

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
```

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
account and `trade_mode` reads `demo` **from the broker**, not from the app
label. The bot refuses to start if these disagree; confirm it refuses if you
deliberately set the app to `real` against the demo terminal.

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
