# Phase A handover

Capital protection, accounting, order lifecycle and the operational UI needed
to report them. Written so the next session can start from here rather than
from a restated master prompt.

Base commit: `1116af1b4972de57cc9c550ab023efff8332e72d` (history filtering,
pagination, close reasons, open exposure panel). Earlier: `0b86408` initial
protections, `274aa43` test isolation.

## What was wrong, and what replaced it

| Defect | Where it was | What it does now |
| --- | --- | --- |
| The daily loss limit read `_day_realized`: an in-memory float, incremented from whatever `close_position()` returned, reset to 0 by a restart, blind to every open position. | `grid_engine._check_risk_limits` | Judged on a **marked** reading: settled trades + change in open mark since a persisted day anchor + anything awaiting settlement. `app/engine/risk_accounting.py` |
| A basket stop called `_close_everything` once. If closes failed and the price recovered, nothing retried — and the UI had already been handed a hardcoded empty position list. | `grid_engine._tick`, basket-stop branch | A persisted `CloseIntent`. A decision, not a condition: driven to broker-confirmed flat, survives restart, unaffected by price recovery. `app/engine/lifecycle.py` |
| `stop()` cancelled the loop. Target, stop, daily limit and drawdown all stopped being evaluated while positions stayed live, and the UI said only "no new trades will be opened". | `grid_engine.stop`, `App.jsx` | Three separate actions: **Pause entries** (keeps protecting), **Close positions** (flattens this bot's own exposure), **Stop** (ends monitoring, and the response states exactly what was left open). |
| `_estimated_exit_cost` returned `0.0` when the symbol read failed — least conservative exactly when data was worst. `costs_known` was computed and ignored. | `grid_engine` | Returns `None` for unknown. The profit target requires known costs *and* an estimable reserve; a loss exit is never suppressed by missing data. |
| `_affordability` read `getattr(account, "free_margin", None)` — a field `AccountInfo` never had, so the margin check passed every time. | `grid_engine._affordability` | `AccountInfo` carries real `free_margin`, `margin`, `margin_level`, `trade_allowed`, `trade_mode`, `broker_id`, all `None` when unknown. Margin is reserved for the whole proposed batch plus resting orders via `calc_margin`, and an unknown refuses. |
| The risk key included the app's `mode` label, so a demo/real toggle selected a different record and handed the account a fresh budget. | `grid_engine._risk_key` | Keyed by broker account identity, symbol and magic. Legacy mode-qualified keys are migrated conservatively (an unresolved halt wins). |
| Persistence failures were logged and swallowed. A failed read read as "no halt". | `grid_engine`, `db.save_risk` | `db.PersistenceError`. A failed write blocks new entries and stays visible; a failed read blocks entries rather than being taken as proof of safety. |
| `start()` checked the app's mode string. `trade_mode` and `hedging` were populated by the MT5 adapter and read by nothing. | `grid_engine.start`, `routes` | `verify_account` runs on `/api/start`, `/api/test-order` and every entry gate. A real account behind a demo label is refused; an unclassified account is refused. |
| History and analytics ran before protection in `_tick`. | `grid_engine._tick` | An outstanding close intent is driven first; a full history sweep never sits between a breach and the order that ends it. |
| A bare `AccountInfo` had no persistent floor; the reserve percentage was doing two jobs. | `config`, `grid_engine` | `GRID_CAPITAL_FLOOR_USD` is a separate persistent line. 0 means unset and blocks new entries. |

## What is NOT done

- **No broker-side stop loss.** Every protection runs in the Python process. If
  that process dies, nothing in this bot protects the account. This is an
  unresolved owner decision: the original specification asked for individual SL
  where feasible, and the locked strategy forbids per-trade SL/TP. It was not
  added silently either way.
- **`SymbolInfo.profit_includes_exit_spread` is `None` for every adapter.**
  Whether MT5's floating profit is already struck at the executable closing side
  is not verified here. While it is unverified the exit reserve tightens the
  profit target (delaying a close is safe) and is deliberately **not** used to
  bring a loss exit forward.
- **No MT5 verification of anything.** No terminal was contacted. Fill
  behaviour, real spread, latency, margin semantics and hedged-margin relief all
  require separate Windows verification.
- Phases B (execution/latency), C (research/profiles) and D (data/ML) are
  untouched.

## Owner choices that block activation

Software is not waiting on these; **new trading** is.

| Setting | State |
| --- | --- |
| `GRID_BASKET_STOP_LOSS_USD` | must be > 0, or a daily limit must be |
| `GRID_MAX_DAILY_LOSS_USD` | must be > 0, or a basket stop must be |
| `GRID_CAPITAL_FLOOR_USD` | **unset (0)** — blocks new entries until chosen |
| Broker-side SL | unresolved conflict, above |

A basket stop must also be large enough for the configured grid. With 10+10 at
0.01 and 0.30 spacing the completed-grid scenario is about **-$37.80**, so a
stop below that is refused with a stated reason. That refusal is the admission
policy working, not a bug — and the figure is **one scenario, not a proven
maximum loss**: it does not cover gaps or one-sided runs.

## Tests actually run

In this Linux container, against injected doubles and the mock broker:

```
python3 -m pytest -q      ->  125 passed
npm test                  ->  4 passed
npm run lint              ->  0 errors, 4 warnings (pre-existing data-fetch effects)
npm run build             ->  clean
```

`tests/test_phase_a_acceptance.py` (38 cases) covers the acceptance list,
including a **subprocess** recovery test that rebuilds the engine in a separate
interpreter against the same database file — an in-process object rebuild is not
evidence of crash recovery. A passing count proves the logic exercised; it
proves nothing about MT5 execution or profitability.

## Operator transition when an update stops monitoring

Do not restart blind over open exposure. In order:

1. **Pause entries** on the dashboard. Nothing new opens; existing positions
   stay monitored.
2. Wait for the basket to close on its own, or press **Close positions** and
   wait for the panel to confirm zero positions and zero resting orders.
3. Only then stop the backend and update. Stopping while exposure is open means
   nothing is watching it — the Stop dialog says so, and the response reports
   what was left.

## Next task

Phase B brief: measure quote age, decision time, dispatch, acknowledgement,
first fill, final closure and UI delivery **separately**; move history and
analytics off the protection path entirely; one order owner, bounded queues,
broker reconciliation, crash recovery. Decide Python vs native MQL5 executor on
those measurements, not on preference.
