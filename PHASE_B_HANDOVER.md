# Phase B handover — measured execution separation

Base: `b1ba98c` (Phase A). All Phase A prerequisites were re-verified by
running their tests before any change: floating-loss daily accounting, restart
persistence, close retries, pause behaviour, margin admission and account
identity — 9 targeted tests, all passing.

## The defect this phase addresses

Protection ran **behind** reporting. One combined `_tick` did, in order:
record new fills to the database, sweep the broker's settlement history, read
realised profit per closed ticket, read candles for the chart — and only then
check whether a stop should fire. A full history sweep sat between a breach and
the order that ends it, on every cycle where its interval came round.

## Measurement

Synthetic, in-process, no terminal. Per-call delays are **stated, not derived
from the owner's account**: account 5 ms, positions 12 ms, pendings 5 ms,
candles 25 ms, symbol info 3 ms, realised profit 8 ms/ticket, history sweep
120 ms.

| | before | after (protective path) |
| --- | --- | --- |
| median | **63.0 ms** | **25.6 ms** |
| max | 327.2 ms | 248.7 ms |
| `get_open_positions` per cycle | 2.17 | 1.15 |
| `get_candles` per cycle | 1.00 | **0** |

**59% of the median protective path removed.** The remaining max is the close
itself — cancelling 20 orders and closing 10 positions — which is real work,
not avoidable delay.

Sustained 8.1 s run at a 10 ms protective cadence: **0.23 s CPU (3% of one
core)**, 104 MB RSS, recorder buckets bounded and flat (230 samples for 230
cycles — no growth). 230 protective cycles against 38 reporting cycles, which
is the separation working.

**Responsiveness budget:** protective cadence **1.0 s** (`protective_poll_seconds`),
reporting **5.0 s**. One protective cycle costs ~26 ms of in-process work, so a
1 s cadence leaves the loop ~97% idle while still noticing a breach within a
second of observing it. No millisecond fill time is promised anywhere — fill
timing is a broker property this repository has never measured.

## Does the faster cadence help? (exit-only, identical inputs)

Old = decide every 20 ticks; new = every 2. Same positions, ticks and cost model.

| scenario | limit overshoot old → new | ticks to confirmed flat |
| --- | --- | --- |
| steady adverse move | 3.00 → 1.40 | 21 → 19 |
| adverse then reversal | 49.20 → **7.20** | 21 → 7 |
| spread widens before the decision | 3.30 → 0.30 | 41 → 31 |
| terminal unreachable across the window | 4.50 → 0.30 | 61 → 47 |
| rejected closes + partial fills | 51.00 → **6.20** | 101 → 27 |
| 1 tick in 4 never observed | 113.40 → 1.40 | **never flat** → 19 |

The last row is the important one and it is an **adverse finding about the old
policy**: its 20-tick cadence aliased with the observation gap, never landed on
a visible tick, and left 10 positions open at −128.40 marked. Its `net_result`
of 0.00 is unrealised, not a better outcome.

**None of this is evidence of profitability.** Fills are simulated under a
stated model. A sampled tick list is a sample — anything between two samples did
not happen in this model — and candle OHLC cannot order intrabar events, so no
path is inferred from a bar. These fixtures validate mechanics. **Net expectancy
remains unresolved.**

## What changed

- `app/engine/instrumentation.py` — monotonic timing, correlation ids,
  percentiles that withhold a p95/p99 when the sample is too small, and
  censored samples kept rather than dropped. Quote age is local-clock only and
  is explicitly not labelled latency.
- `app/engine/broker_owner.py` — one owner for broker access. Protective work
  takes priority; reporting raises `SkipReporting` rather than returning a
  value that looks like "nothing there". Stall detection reports blocked.
- `grid_engine._protective_tick()` / `_reporting_tick()` — the split. `_tick()`
  composes both, so the whole regression suite exercises the code the loop runs
  rather than a second implementation only tests see.
- `_loop` — separate cadences with bounded backoff (max 8× on error).
- `max_open_positions` capping and the daily-target cancellation both moved to
  where they belong; the daily target is now checked **before** the "something
  is already resting" return, so reaching it cancels the resting grid.
- Snapshot delivery is defensive: a websocket consumer that raises no longer
  takes the engine's cycle with it.
- `app/backtest/execution_replay.py` + `tests/bench/` — the comparison above.

## Limits, stated rather than disguised

- **An in-flight broker call cannot be cancelled.** Moving a synchronous MT5
  call to a thread leaves the thread stuck in the terminal. When a call passes
  `broker_stall_after_ms` the bot reports itself blocked and refuses new
  exposure; it never fires a replacement, because two live requests for one
  decision is worse than waiting.
- **The priority mechanism is not exercised by the async loop alone.** Within
  one event loop protective and reporting work run sequentially. Priority
  matters against the REST endpoints, which read the broker from FastAPI's
  threadpool concurrently. The sustained run shows `protective_waits: 0`
  precisely because nothing competed — that is an honest zero, not a benefit.
- **Polling sees the last tick, not every tick.** `symbol_info_tick` returns
  the most recent tick and can return None. No claim is made that every
  transient profit opportunity is observed.
- **No MT5 verification.** Every number here is in-process against doubles.

## Still true from Phase A

There is **no broker-side stop loss**. All protection runs in this Python
process; if it is killed, nothing in this bot protects the account. That
remains an unresolved owner decision.

`GRID_CAPITAL_FLOOR_USD` is still unset (0), which blocks new entries by design.

## Tests actually run

```
python3 -m pytest -q   ->  150 passed   (125 Phase A + 25 Phase B)
npm test               ->  4 passed
npm run lint           ->  0 errors, 4 warnings (pre-existing)
npm run build          ->  clean
python3 -m tests.bench.bench_execution    (timing)
python3 -m tests.bench.replay_policies    (policy comparison)
```

Behavioural tests use deterministic fixtures with no sleeps or timing
assertions; timing claims live only in the benchmarks.

## Windows PowerShell

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
.\venv\Scripts\python.exe -m tests.bench.bench_execution
cd ..\frontend
npm ci; npm test; npm run lint; npm run build
```

Rollback: `git checkout b1ba98c -- backend frontend` then restore the three
`.bak` files.

**Before updating:** press **Pause entries**, then wait for the open-trades
panel to show zero positions and zero resting orders — or press **Close
positions** and wait for the same confirmation. Do not stop the backend while
exposure is open; it is the only thing watching it.

### Demo verification procedure (for the owner — not run here)

1. Funded demo, limits and capital floor set, Start.
2. Confirm the Grid panel's **Execution health** block shows the protective
   cadence and a local quote age in tens of milliseconds, not seconds.
3. Close the MT5 terminal while a basket is open. Within
   `broker_stall_after_ms` the dashboard should show the blocked banner and
   refuse new entries. Reopen it and confirm the banner clears.
4. Compare `execution_timing` in `/api/status` against the offline medians.
   Real broker call durations will be larger; that difference is the number
   this repository has never had.

## Next task

Phase C is a strategy-evaluation task, not more execution work. The failure
pattern to build it around is the one Phase A already established and Phase B
did not change: **a completed two-sided grid freezes near −$37.80 and cannot
reach its target from there.** Faster closure bounds the loss; it does not make
the structure profitable. The next task should measure how often a basket
reaches that frozen state versus reaching its target, on real tick data, before
any new entry rule is proposed.
