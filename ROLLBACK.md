# Rolling this round back

This round is exactly one commit, `c09030c`, on top of `8319176` — the revision
the independent review examined. Nothing else was touched, so the rollback is
correspondingly narrow.

## What it changed

```
backend/app/engine/grid_engine.py        the floor trigger, liquidation policy,
                                         cost contract wiring, staged reporting,
                                         raw decision values
backend/app/engine/lifecycle.py          CAUSE_CAPITAL_FLOOR, LiquidationPolicy
backend/app/engine/costs.py              NEW — the single closing-cost contract
backend/app/engine/grid_math.py          Estimate.valid / .problem
backend/app/brokers/base.py              SymbolInfo validity + stop attribution
backend/app/brokers/mt5_broker.py        validate valuation inputs, no defaults
backend/app/brokers/mock_broker.py       states its own exit-spread semantic
backend/app/config.py                    3 cost settings + reporting budget
backend/app/bot_manager.py               threads those settings through
backend/app/api/routes.py                card uses the display (rounded) figures
backend/.env.example                     documents all four new settings
backend/tests/…                          4 new files, 4 updated
DEMO_VALIDATION_STATUS.md, OWNER_RUNBOOK.md
```

Your `.env`, `runtime_settings.json` and `trading_bot.db` were **not** touched,
and no schema changed this round.

## Undo the whole round, keep everything before it

```powershell
cd C:\Users\Home\Documents\basit-qureshi
git log --oneline -3
git revert --no-edit c09030c
```

`revert` keeps the history and every earlier commit intact. Use it rather than
`reset --hard`, which would throw away the round instead of recording that you
undid it.

## Undo only part of it

Each finding is separable. To drop just one, revert the commit and re-apply the
parts you want, or check out single files from it:

```powershell
git revert --no-commit c09030c
git checkout c09030c -- backend/app/engine/costs.py backend/tests/test_closing_costs.py
git commit -m "Keep the closing-cost contract, drop the rest of the round"
```

## What breaks if you revert

- the capital floor goes back to being an entry rule only: equity below the floor
  with positions open will not liquidate or halt
- a late fill after a loss stop stays open until another threshold fires
- a broker reporting no tick value goes back to being priced at 1.00 per point
- the daily limit stops counting a known closing cost
- slow reporting can set the protective cadence again
- 9.996 clears a 10.00 target again
- the three cost settings and `REPORTING_TIME_BUDGET_MS` become unused; leaving
  them in `.env` is harmless

## Verify whichever state you land on

```powershell
cd C:\Users\Home\Documents\basit-qureshi\backend
.\venv\Scripts\python.exe -m pytest -q
```

477 passed at `c09030c`. After a full revert, expect the previous count (404) and
the four new test files to fail or vanish with it.
