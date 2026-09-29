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
Expect **494 passed**. Anything else, stop and send the output.

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

Every line below is a **single line**. Do not break them: `^` is cmd.exe's
continuation and does nothing useful in PowerShell, and an earlier revision of
this runbook wrongly used it.

```powershell
cd C:\Users\Home\Documents\basit-qureshi\backend
.\venv\Scripts\python.exe tools\grid_fit_report.py --assumptions
```

With your own numbers, still one line each:

```powershell
.\venv\Scripts\python.exe tools\grid_fit_report.py --spread 0.25 --broker-stop-level-points 0 --basket-stop 60 --daily-loss 100 --balance 1000 --floor 800
.\venv\Scripts\python.exe tools\grid_fit_report.py --spread 0.25 --commission-per-lot-per-side 2.75 --slippage-points-per-fill 1 --basket-stop 60
.\venv\Scripts\python.exe tools\grid_fit_report.py --alternatives --spread 0.25
```

It reads nothing, opens no terminal and cannot place an order. Every input is
printed back with its provenance, so a FIXTURE value is never mistaken for your
broker's.

**Where the first level's distance comes from.** The report splits it three ways,
because only one of the three is a broker rule:

| Term | Whose | How it is obtained |
| --- | --- | --- |
| `trade_stops_level × point` | **the broker's** | `--broker-stop-level-points`, or read it off `/api/status` → `stop_distance.broker_stop_level_distance` |
| `+ 5 points` | **this application's** | a buffer against rounding and price movement between calculating a stop and submitting it. Applied only when the broker declares a minimum |
| `spread × 3` | **this application's** | a fallback for brokers that declare no minimum yet still reject a stop inside the live spread. **No broker states this rule** |

The engine places the first level at `max(grid distance, max(broker + buffer,
spread × 3))`. On gold at a 0.24 spread the third term wins — `0.72`, wider than
the `0.30` grid distance — so an **application choice**, not a broker
requirement, is what moves every level and inflates the estimate:

| Effective first step | Where it came from | Estimate |
| --- | --- | --- |
| 0.30 | the grid distance (test fixture: the double reports no minimum at all) | **37.80** |
| 0.72 | this app's spread × 3 at a 0.24 spread | **46.20** |
| 1.05 | a broker declaring 100 points, plus this app's 5 | **52.80** |

Read your live split while the backend is running, one line:

```powershell
(Invoke-RestMethod http://127.0.0.1:8000/api/status).stop_distance
(Invoke-RestMethod http://127.0.0.1:8000/api/status).completed_grid_estimate
```

**What the estimate covers, and what it does not.** One scenario: every level
filled, equal volume both sides, entry spread once per fill, valued as if closed
back at the reference price. It is **not a maximum loss**. Closing costs are not
in it — no exit spread, no commission, no swap, no slippage — and it says nothing
about partial fills, unequal fills or one-directional exposure, which is not
frozen and is bounded by the basket stop instead.

**So a passing comparison is not budget compatibility.** Admission compares the
entry-side estimate against a budget and nothing more; the remaining-budget
figure it compares against excludes the exit reserve too. Neither side carries a
closing-cost buffer. The report now says so, and where a closing cost has not
been supplied it prints **UNKNOWN** rather than zero and refuses to total it.
Supply your own figures with `--commission-per-lot-per-side` and
`--slippage-points-per-fill` if you want them counted; nothing invents a fee, and
swap stays unknown because it depends on nights held.

**If the grid does not fit the limits you choose, the rejection stands.** Nothing
here advises raising a limit or adding money to clear a gate. `--alternatives`
prints smaller profiles as arithmetic for a decision you make later: untested,
not active, and not known to be better — a smaller grid has a smaller adverse
scenario *and* a smaller cash target.

**These changes improve prevention and recovery. They do not make a limit
absolute.** A gap, a rejected close, a terminal that stops answering, or movement
between two protective cycles can still overshoot a trigger.

## B. Requires the MT5 terminal — you run these, not the development environment

### B1. Export ticks (reads history, cannot place an order)

**This is the next thing worth doing, and it waits on nothing.** It does not need
your risk numbers: a read-only export cannot open a position. Verified offline —
the tool imports nothing from `app`, so no engine and no order path is reachable
from it, and the only terminal calls it makes are `initialize`, `symbol_select`,
`copy_ticks_range`, `version` and `shutdown`. `tests/test_export_ticks.py`
asserts that against a fake MT5 module (17 tests).

It does need the MT5 terminal open and logged in, because only the terminal holds
the history.

**One command, one line. Copy it exactly:**

```powershell
cd C:\Users\Home\Documents\basit-qureshi\backend; New-Item -ItemType Directory -Force -Path data | Out-Null; .\venv\Scripts\python.exe tools\export_ticks.py --symbol XAUUSDm --from 2026-06-25 --to 2026-09-24 --out data\xauusdm_ticks.csv
```

If it stops part way — terminal restart, connection drop — continue it with the
same line plus `--resume`:

```powershell
.\venv\Scripts\python.exe tools\export_ticks.py --symbol XAUUSDm --from 2026-06-25 --to 2026-09-24 --out data\xauusdm_ticks.csv --resume
```

Then read the manifest, one line each:

```powershell
(Get-Content data\xauusdm_ticks.csv.manifest.json | ConvertFrom-Json) | Select-Object rows_total_in_file, rows_skipped_no_finite_two_sided_quote, rows_out_of_order, days_with_no_rows_expected_closed, coverage_is_complete
(Get-Content data\xauusdm_ticks.csv.manifest.json | ConvertFrom-Json).unexplained_gaps
```

That exact argv was run offline against a fake terminal over a Thursday–Monday
range: five inclusive days, five bounded requests, 15 rows, the weekend
classified as an ordinary closure, `unexplained_gaps` empty, manifest written to
`data\xauusdm_ticks.csv.manifest.json`. The regression is
`test_the_documented_single_line_command_works_end_to_end`.

**Real flags, as the file defines them:** `--symbol`, `--from`, `--to`, `--out`
(required), then `--chunk-days` (default 1), `--resume`, `--overwrite`,
`--manifest`. There is no other flag.

**Date boundaries.** Both dates are **inclusive UTC days**. `--to 2026-09-24`
covers through 2026-09-24 23:59:59.999 UTC; internally the request runs to the
start of the 25th. An earlier version treated `--to` as a midnight boundary, so
the last day came back empty — that is fixed and tested. A reversed range is
refused. Each day is a separate bounded request, so memory stays flat and a
partial run resumes cleanly with a monotonic `seq`.

**Manifest handling.** A successful run is **not** proof of coverage, so read
these fields:

| Field | What it means |
| --- | --- |
| `requested_range_utc` | what you asked for — unchanged by `--resume` |
| `covered_this_run_utc` | what this run actually fetched, with `resumed: true/false` |
| `rows_total_in_file` | rows in the CSV, across resumes |
| `rows_skipped_no_finite_two_sided_quote` | rows with no finite bid **and** ask. Skipped and counted, never silently dropped |
| `rows_out_of_order` | non-monotonic timestamps; a non-zero value means the file is not sorted |
| `days_with_no_rows_expected_closed` | Saturdays and Sundays with no ticks — ordinary market closure |
| `unexplained_gaps` | **weekdays** that came back empty. Non-empty means coverage is incomplete |
| `coverage_is_complete` | false if there were errors or out-of-order rows |

Weekend classification is coarse and deliberately errs toward alarm: a public
holiday shows up as an unexplained gap. Nothing is ever interpolated — a gap is
reported, never filled.

**What the file must contain:** `time_utc,bid,ask` plus `last,volume,flags,seq`;
**bid and ask on every row** (a mid-only export cannot price a strategy that pays
the spread on up to 20 fills against a fixed cash target); the **exact symbol
including the broker suffix**; **tick resolution, not M1 bars**. Two ticks sharing
a millisecond are both kept, told apart by `seq`.

**About the range.** Three months is a starting collection window, not a
statistically sufficient sample, and nothing here assumes your broker retains
that much tick history. Export what exists, send the manifest with the CSV, and
let the coverage decide what can be measured. If the history is short, the answer
is prospective collection from now on — not a bigger claim from less data.

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

## D0. DEMO TEST — start to finish, in order

This is the section to follow. Everything else in this file is reference.

### Step 1 — pre-flight. Reads your terminal, places no orders.

Open the MT5 terminal, log into the **demo** account, then one line:

```powershell
cd C:\Users\Home\Documents\basit-qureshi\backend; .\venv\Scripts\python.exe tools\demo_preflight.py
```

It prints **your** numbers, not a fixture's: the broker's own demo/real
classification, the symbol's point value, the live spread, which rule sets the
first level's distance (the broker's or this app's), your completed-grid
estimate, and the commission it can measure from your own closed deals. Then it
lists the settings that are still unset with the exact `.env` lines to add.

It **refuses to read anything further if the terminal is logged into a REAL
account**, and there is no order call anywhere in the file — 17 tests assert
both against a fake terminal.

### Step 2 — fill in the settings it lists.

Edit `backend\.env`, add the lines it printed, and restart the backend. Two of
them are yours to decide and the tool will not decide them for you:

| Setting | What it means |
| --- | --- |
| `GRID_BASKET_STOP_LOSS_USD` | the most one basket may lose before it is closed. Must exceed your completed-grid estimate, or admission refuses every grid and says so |
| `GRID_MAX_DAILY_LOSS_USD` | the most the day may lose, judged on the **marked** result with floating loss included |
| `GRID_CAPITAL_FLOOR_USD` | the balance this account is never traded below. Also an **active trigger**: if equity reaches it while the bot owns exposure, the bot closes **its own** positions and latches entries |

And three costs, where the honest default is stated:

| Setting | Where it comes from |
| --- | --- |
| `EXIT_COMMISSION_PER_LOT_USD` | the pre-flight measures it from your closed deals. No deals yet? Place **one** manual trade by hand, close it, run the pre-flight again |
| `SLIPPAGE_POINTS_PER_FILL` | what you have actually observed. Put `0` only if you have looked and seen none |
| `BROKER_PROFIT_INCLUDES_EXIT_SPREAD` | **if you do not know, put `no`.** That is the conservative side: it can only make the bot stricter, never looser |

Run the pre-flight again. It should end with `Every check this tool can make has
passed`.

### Step 3 — start the backend and the dashboard. This does not start trading.

```powershell
cd C:\Users\Home\Documents\basit-qureshi\backend; .\venv\Scripts\python.exe run.py
```
Second window:
```powershell
cd C:\Users\Home\Documents\basit-qureshi\frontend; npm run dev
```

Check the dashboard **before** pressing Start:

- the badge says **DEMO**
- `account_verified` matches your demo account and `broker_trade_mode` reads
  `demo` — the broker's word, not the app's setting
- the Grid panel shows your three risk numbers and the day's marked reading
- no entry-block reason is showing. If one is, it names exactly what is missing

### Step 4 — run it, and watch these four things.

Press **Start**. During the run, the dashboard tells you the truth about:

| What you see | What it means |
| --- | --- |
| **⏸ Entries paused** | new grids stopped; open positions are still managed |
| **🛑 Holding exposure at zero after …** | a loss stop left a standing instruction. Every late fill will be closed again. **Only Resume entries clears it** |
| **⏳ Closing (state, attempt n)** | a close was ordered and is not confirmed yet. "Sent" is not "gone" |
| **unknown** anywhere in the exposure panel | the broker could not be read. It is not zero, and the bot will not treat it as zero |

### Step 5 — stop it safely. Read this before you close anything.

**There is no broker-hosted stop loss.** No SL or TP is attached to any order,
so if the backend stops, nothing closes anything.

1. Press **Pause entries**
2. Wait for **0 positions AND 0 resting orders** — or press **Close positions**
   and wait for the same confirmation. `unknown` is not zero
3. Only then stop the backend

Never kill the process to end a session while anything is open.

### Step 6 — keep the record.

```powershell
$api = "http://127.0.0.1:8000/api"
Invoke-RestMethod -Method Post $api/session/start | ConvertTo-Json -Depth 5
```
Run that **before** Start in step 4 if you want the session recorded, then after
step 5:
```powershell
Invoke-RestMethod -Method Post $api/session/stop | ConvertTo-Json -Depth 5
.\venv\Scripts\python.exe tools\export_session.py --list
```
§F has the full procedure and the redaction rules.

### What this demo run will and will not tell you

It **will** tell you whether the machinery works on a real terminal: does it
place the grid it calculated, does it close when it says it closes, does the
dashboard match MT5, does a halt survive a restart.

It will **not** tell you whether the strategy makes money. A handful of baskets
on one stretch of market cannot separate skill from luck, and the strategy's
central risk — both sides filling and the basket freezing near your
completed-grid estimate — has never been measured on real price history. For
that, §B1's tick export is still the only answer.

---

## D. Controlled demo verification — the deeper checklist

§D0 above is the path to follow for a first demo run. This section is the fuller
set of checks worth doing once it is running: ownership scope, pause semantics,
one supervised lifecycle, truthful exposure under a disconnect, and recovery
across a restart. None of it has been run yet.

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

> **Since the independent review, two things changed for you.**
>
> **1. The capital floor now acts, not only refuses.** If account equity reaches
> it while this bot owns positions or orders, the bot cancels and closes **what it
> owns**, latches entries, and holds its exposure at zero until you resume. Equity
> includes trades this bot does not own, so a manual loss can trigger it — and
> flattening this bot cannot repair that; it removes its own contribution and
> stands down. With nothing of its own open the floor is an entry refusal only.
> It is a trigger, not a guaranteed final balance.
>
> **2. Closing costs must be stated or new entries are blocked.** Three new
> settings, all UNKNOWN by default and never assumed to be zero:
>
> | Setting | Where to get it |
> | --- | --- |
> | `EXIT_COMMISSION_PER_LOT_USD` | your contract specification or an account statement |
> | `SLIPPAGE_POINTS_PER_FILL` | slippage you have actually observed on this symbol |
> | `BROKER_PROFIT_INCLUDES_EXIT_SPREAD` | whether MT5's floating profit for your broker is already struck at the closing side. Leave `unverified` until you have checked — while unverified, the exit spread can delay a profit exit but cannot fire a loss exit early |
>
> Existing positions stay protected either way; this gate only governs new
> baskets. A read-only tick export (§B1) does not depend on any of it.

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
> | spread | effective first step | whose rule set it | estimate |
> | --- | --- | --- | --- |
> | 0.24 | 0.30 | the grid distance (fixture: the double declares no minimum) | 37.80 |
> | 0.24 | 0.72 | **this app's** spread x 3 | 46.20 |
> | 0.25 | 0.75 | **this app's** spread x 3 | 47.00 |
> | 0.50 | 1.50 | **this app's** spread x 3 | 67.00 |
> | 0.24 | 1.05 | a broker declaring 100 points, **plus this app's 5** | 52.80 |
>
> Only the first row is a pure broker/configuration outcome. In the others an
> application choice is what moved the levels — see §A7 for the split.
>
> Get your own number offline, with no terminal and no risk (§A7):
>
> ```powershell
> .\venv\Scripts\python.exe tools\grid_fit_report.py --assumptions --spread 0.25 --basket-stop 60 --daily-loss 100
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
