# Phase C handover — entry selection and profit protection research

Base: `83b81f5` (Phase B). Branch `claude/forex-ai-trading-bot-izgn6l`.

## Selection result

**INSUFFICIENT EVIDENCE — BLOCKED BY DATA.**

No candidate was selected, nothing was frozen, and the final evaluation window
was never opened. There is no market data in this repository to open it with.

---

## 1. What the strategy actually is

Read from the code, not from its "Grid" label. `_build_grid` places BUY STOPs
**above** the reference and SELL STOPs **below** it. A buy stop fills when price
*rises* into it; a sell stop fills when price *falls* into it. Both sides
therefore fill on movement **away** from where the basket started.

This is a **symmetric breakout straddle**, not a mean-reversion grid.

Computed from the live geometry (10+10, 0.01 lot, 0.30 spacing, 24-point spread):

| one-directional move from the reference | basket net |
| --- | --- |
| +1.0 price units | +$0.48 |
| +2.0 | +$4.26 |
| **+3.0** | **+$11.10** ← clears the $10 target |
| +5.0 | +$31.10 |

| oscillation that fills both sides, price returns | basket net |
| --- | --- |
| full 10+10 filled, back at the reference | **−$37.80, frozen** |

**How it makes money:** one sustained directional move of roughly 2.9 price
units (290 points) before price turns.

**How it loses money:** price oscillates enough to fill both sides and comes
back. The volumes cancel, the price terms drop out, and the basket's value
stops responding to price at all. It cannot reach its target from there by any
means.

**Consequence for filter selection:** the common claim that "a grid needs a
ranging market" is **exactly backwards for this structure**. A tight range is
its failure mode. That is what Candidate 2's hypothesis tests.

## 2. Data — the blocking finding

| needed | present |
| --- | --- |
| XAUUSD(m) tick history, bid **and** ask, UTC | **none** |
| the bot's own trade history (`trading_bot.db`) | **not in this repo** (gitignored, lives on the Windows machine) |
| economic calendar with availability times | **none** |

The only supplied archive is `TWP_SET_FILES_1.14.zip`, which contains EA
presets and vendor tester reports — not market data, and not this bot's data.

A trade report without market data cannot replay what a filter *would* have
done, so no candidate can be evaluated. Selection is blocked until the export
below exists.

**Export command — for you to run on Windows.** It reads history only and
places no orders. Nothing in this repository contacts a terminal.

```powershell
cd C:\Users\Home\Documents\basit-qureshi\backend
.\venv\Scripts\python.exe tools\export_ticks.py --symbol XAUUSDm `
    --from 2026-06-01 --to 2026-09-20 --out data\xauusdm_ticks.csv
```

Required: `time_utc,bid,ask` (optional `last,volume,flags`), at least **3
months continuous**, the **same symbol including its broker suffix**, tick
resolution not M1 bars. A mid-only export is refused by the loader, because
this strategy pays the spread on up to 20 fills against a fixed cash target.

## 3. Candidates implemented (all disabled for live trading)

| profile | hypothesis | falsified if |
| --- | --- | --- |
| `research-execution-quality@v1` | A meaningful share of losses come from starting baskets on a wide spread or a stale quote. At 0.01 lots a 24-point spread costs ~$6.00 across a full grid — 60% of the $10 target — before price moves. | filtering those out does not reduce net loss per basket |
| `research-regime@v1` | This straddle needs movement; a tight range is what freezes it. Admits only when closed-bar ATR is large enough relative to spacing. | admitting only in the expanding regime does not improve result per basket |
| `research-event-blackout@v1` | Starting a basket around a scheduled high-impact release is worse than average. Uses **scheduled timing only** — it predicts no direction. | excluding those windows does not reduce loss per basket or drawdown |
| `research-trailing-exit@v1` | The fixed $10 target leaves money on the table; a giveback rule arming **above** it captures more. | net per basket does not improve, or drawdown/time-exposed worsens |
| `research-execution-and-regime@v1` | The one combination, tested only after both individuals. | — |

`baseline@v1` is unchanged: no gates, no trailing, same geometry, same target,
same loss budgets. **Grid geometry was not changed anywhere in this phase.**

Every research profile has `live_enabled = False`, asserted by a test.

## 4. Causality and hygiene, enforced in code

- **Three-valued admission.** `ALLOW / BLOCK / UNKNOWN`. Collapsing unknown into
  allow trades on absent data; collapsing it into block makes a broken feed
  indistinguishable from a real veto. Gates marked `data_required` block on
  unknown — so the event gate stops admission entirely while no calendar exists,
  rather than trading as though the diary were empty.
- **Gates report `inputs_as_of`,** not their own run time, so a decision taken
  on a ten-minute-old quote cannot present itself as fresh.
- **Closed bars only.** `RegimeGate` takes a closed-bar list and has no access
  to the forming bar. ATR returns `None` during warmup instead of a padded
  value. A test feeds an absurd current bar and asserts the verdict is unchanged.
- **Calendar availability time is separate from event time.** Every query is
  "what did the diary say *as of* this instant", so a replay cannot see an entry
  published later. A test asserts an entry published one minute before the
  decision is invisible to a decision taken earlier.
- **No ideal fills.** A level crossed between two observed ticks does not fill
  at that level; the fill is marked `gapped` and priced at the observed tick,
  which is worse and honest. The replay refuses to run on candle OHLC at all,
  because bars cannot order intrabar events.
- **Open exposure is marked and disclosed, never dropped.** Dropping it would
  remove exactly the baskets that never recovered.
- **Time-based splits with purge and embargo.** Equal row counts are not equal
  elapsed time for irregular ticks. Baskets straddling a boundary are purged;
  an embargo excludes baskets opened just before one.
- **Sessions, not tickets.** Twenty tickets in one basket are one correlated
  observation. `independent_sessions` counts sessions and is labelled a crude
  proxy wherever reported.
- **The final window opens once.** `ExperimentLog.freeze` locks a selection, and
  a second `open_final` for a different candidate raises and records a refusal.
- **Trailing cannot manufacture a peak.** A valuation with unreported costs does
  not raise the peak, because an inflated peak would set the giveback floor too
  high and hold a basket open on money that was never there.

## 5. Synthetic run — mechanics only, no edge

`python3 -m tests.bench.run_phase_c`

```
profile                           done      net  open  open mkd  blocked    worst
baseline                             0     0.00     1    -24.58        0   -39.00
research-execution-quality           0     0.00     1    -24.58        0   -39.00
research-regime                      1    10.05     1    -33.46      601   -37.68
research-event-blackout              0     0.00     0      0.00     5700     0.00
research-trailing-exit               0     0.00     1    -24.58        0   -39.00
research-execution-and-regime        1    10.04     1    -35.82      625   -40.43
```

**Read this correctly.** `research-regime`'s `+10.05` comes with an open basket
marked at **−$33.46**. It is not ahead. `baseline`'s `0.00` is not break-even —
it is one basket still open at **−$24.58**. And the event-blackout row blocking
all 5,700 admissions is the `data_required` rule working, not a result.

**One basket is one observation.** Nothing here supports a selection, and no
claim of improvement is made from it. What this run does establish is that the
machinery runs end to end, that the gates block for the reasons they state, and
that the known failure mode reproduces: the baseline basket filled 18 of 20
levels, peaked at **+$5.63** — never reaching the $10 target — sank to −$39.00,
and ended stuck.

## 6. Tests actually run

```
python3 -m pytest -q   ->  196 passed   (150 prior + 46 Phase C)
npm test               ->  4 passed
npm run lint           ->  0 errors, 4 warnings (pre-existing)
npm run build          ->  clean
python3 -m tests.bench.run_phase_c
```

Covered: future-data leakage, incomplete/forming candles, calendar availability
timing, missing and stale inputs, duplicate and out-of-order ticks, restart of
armed trailing state, basket identity isolation, unknown-cost handling, purge
and embargo, session counting, final-period isolation, and mid-basket profile
independence. Phase A and B regressions all retained and passing.

## 7. Separated claims

| | status |
| --- | --- |
| Mechanics implemented and tested | **done, offline** |
| Real historical evidence | **none — no data** |
| Synthetic evidence | mechanics only; no edge established |
| Windows / broker verification | **still required** |
| Profitability | **not established, for the baseline or any candidate** |

Still true from earlier phases: **there is no broker-side stop loss**, so all
protection dies with the Python process; and `GRID_CAPITAL_FLOOR_USD` is unset,
which blocks new entries by design.

## 8. PowerShell

```powershell
cd C:\Users\Home\Documents\basit-qureshi
Copy-Item backend\.env backend\.env.bak -Force
Copy-Item backend\runtime_settings.json backend\runtime_settings.bak.json -Force
Copy-Item backend\trading_bot.db backend\trading_bot.bak.db -Force
git status
git config pull.ff only
git pull origin claude/forex-ai-trading-bot-izgn6l
cd backend
.\venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-mt5.txt
.\venv\Scripts\python.exe -m pytest -q
.\venv\Scripts\python.exe -m tests.bench.run_phase_c
cd ..\frontend
npm ci; npm test; npm run lint; npm run build
```

Rollback: `git checkout 83b81f5 -- backend frontend` then restore the `.bak` files.

**Before updating:** press **Pause entries**, then wait for the open-trades
panel to confirm zero positions *and* zero resting orders — or press **Close
positions** and wait for the same confirmation. Do not stop the backend while
exposure is open; it is the only thing watching it.

## 9. Next task

**Export the tick data, then answer one question before proposing anything
else:** over real history, how often does a basket reach its target versus how
often does it reach the frozen both-sides-filled state?

That single ratio decides the phase after this. If freezing dominates, no entry
filter fixes it — the geometry is the problem, and the next task is a geometry
or exit-structure question, not a filter question. If freezing is rare and the
losses come from cost or timing instead, Candidates 1 and 2 become worth
evaluating properly against the split plan already built here.

Phase D should build on that ratio. More AI applied to a structure that freezes
is more AI applied to a structure that freezes.
