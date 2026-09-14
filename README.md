# Gold grid bot: audited research build

This is trading research software, not a profitable product certification.
Real trading is disabled by default. Do not enable it based on synthetic tests,
a video, a classifier accuracy score, or a profitable candle backtest.

## Implemented changes

* Broker position/order read failures raise errors instead of returning empty state.
* Account mode, USD currency, and hedging mode are checked against MT5.
* Stop Bot requests basket closure and keeps monitoring until positions and orders are gone.
* Interrupted closures persist and retry after price changes or process restart.
* Pending orders are cancelled before positions are closed.
* Daily loss uses scoped settled results plus floating loss; drawdown high-water state persists.
* Trades are scoped to account, symbol and magic. Duplicate legacy records are retained
  as DUPLICATE and excluded from statistics. Legacy records are not guessed into a real account.
* Unsettled outcomes are retried. Current basket trigger includes reported commission and swap.
* Grid exposure exceeding the position cap is rejected before creation.
* Full grid margin is checked against 80% of free margin on MT5.
* MT5 pending orders carry a disaster stop 5.00 price units away (or farther if broker rules require).
  This is separate from the basket stop and is not a guaranteed basket loss limit.
* Spreads above 0.50 price units block new grids.
* Unprotected test-order endpoint is disabled.
* Local server binds to 127.0.0.1 without reload or multiple workers.
* Candle replay processes one fill/exit event at a time and reports final floating exposure.
* A real local softmax classifier, chronological training/holdout, and optional entry filter are included.

## Strategy and limitations

The legacy grid places 10 buy stops and 10 sell stops, each 0.01 lots, spaced
0.30 price units apart. It checks combined basket results on a polling interval.
Once a basket closes, the next grid waits for a later confirmed M1 candle.
It does not guarantee a settled $10 profit: prices and costs change during closure.

Equal buy and sell volume neutralizes directional price sensitivity, not losses,
spread, margin needs or swap. A completely filled symmetric grid can lock a loss.
The AI filter can select just the buy or sell side, but its value must be measured
against the same strategy with the filter disabled.

Do not assume old MAX_OPEN_TRADES, MAX_DAILY_LOSS_PERCENT, RISK_PERCENT,
STOP_LOSS_PIPS or TAKE_PROFIT_PIPS fields control the grid.
Use GRID_MAX_OPEN_POSITIONS, GRID_MAX_DAILY_LOSS_USD,
GRID_BASKET_STOP_LOSS_USD and GRID_MAX_EQUITY_DRAWDOWN_PERCENT.
Saved backend/runtime_settings.json values override .env grid settings.
The defaults are not a position-size recommendation for your balance.

## Safe update on Windows PowerShell

First use MT5 to verify that the OLD version has no bot positions or pending orders.
The OLD Stop button only stops its loop. Stop the old Python server after verifying flat.

From the repository root:

```powershell
git status
$backupPath = Join-Path $env:TEMP ("trading_backup_" + (Get-Date -Format yyyyMMdd_HHmmss))
New-Item -ItemType Directory -Path $backupPath
Copy-Item backend/.env,backend/runtime_settings.json,backend/trading_bot.db $backupPath -ErrorAction SilentlyContinue
git pull --ff-only origin claude/forex-ai-trading-bot-izgn6l
cd backend
.\venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-mt5.txt
.\venv\Scripts\python.exe -m pytest -q
.\venv\Scripts\python.exe run.py
```

If Git reports local source conflicts, preserve those changes and resolve them;
do not use reset --hard. Back up the SQLite database only after the backend stops.
Use a compatible Windows Python (3.12 recommended for the test environment).

In a second terminal from the repository root:

```powershell
cd frontend
npm install
npm run build
npm run dev
```

Keep MT5 on DEMO. Visit http://localhost:5173 and review the actual grid settings
before clicking Start. Stop now keeps retrying until the broker confirms flat.
No script in the export, training, report or replay tools sends live orders.

## Local AI workflow

No API subscription is required. Training produces JSON coefficients, not a
downloaded executable model. Default AI_MODE=shadow reports model status without
changing entries. AI_MODE=filter blocks entries when a model is missing, stale,
below confidence, mismatched, or fails its research eligibility check.
AI_MODE=off disables the gate.

Run from backend, with MT5 connected and the Python backend stopped:

```powershell
.\venv\Scripts\python.exe -m app.tools.export_market --symbol XAUUSDm
.\venv\Scripts\python.exe -m app.ai.train data/candles.csv --symbol XAUUSDm --cost 0.30
```

Use your observed transaction costs; 0.30 is only an example in price units.
Training uses the first 80% and a purged final 20% holdout. The resulting eligibility
is a directional classifier proxy, NOT grid profitability. The model is only
considered available after its holdout ends, so earlier replay decisions cannot
use future-trained coefficients. Retrain after 30 days.

For a demo experiment after reviewing training output, add AI_MODE=filter to
backend/.env and restart the backend. No training occurs automatically on startup.
Compare filtered and unfiltered results on later unseen ticks, and then forward demo.

## Broker tick replay

```powershell
.\venv\Scripts\python.exe -m app.tools.export_ticks --symbol XAUUSDm --days 7
.\venv\Scripts\python.exe -m app.tools.replay_ticks data/ticks.csv --balance 100 --contract-size 100
.\venv\Scripts\python.exe -m app.tools.replay_ticks data/ticks.csv --balance 100 --contract-size 100 --model models/model.json
```

Set balance and contract size to the account and symbol you are evaluating.
Replay reads runtime_settings.json and runs GridEngine in a temporary isolated
database. It processes Bid/Ask quotes and gap fills on every tick while applying
the configured engine polling interval. It reports ending equity including floating
loss. It does not simulate broker rejection, latency, commission, swap, or margin
stop-out, so even positive results require further validation. The dashboard's
Yahoo gold futures OHLC backtest remains an explicitly labelled approximation.

## Report audit

```powershell
.\venv\Scripts\python.exe -m app.tools.audit_report "C:\path\history.csv"
```

The tool accepts English detailed CSV exports, summarizes monetary columns, and
groups by magic when available. Without magic, trades remain unclassified.
When close deals use a different magic, join them to their opening position ID
before claiming bot-specific performance. A deals export row is not necessarily
one complete trade. Raw reports and trained models stay in ignored local folders.

## Remaining release evidence

The supplied CSV and video could not be opened by the chat's available tools.
No claim is made that their contents were analyzed, or that manual trades were
fully separated. Local active settings and MT5 execution have not been verified
from here. Broker history export, quote data, demo forward results, and the video's
key frames are still needed for that assessment.

Do not enable ALLOW_REAL_TRADING until you have independently verified account,
execution, losses, costs, restart behavior and unseen-data strategy performance.
There is no guaranteed profit setting.

## Tests

GitHub Actions runs Python compilation, pytest, frontend lint and production build.
Regression cases include restart duplicates, persistent loss limits, failed-close
retry, missing model behavior, feature causality, costs, CSV ownership ambiguity,
tick replay isolation and order/target chronology.
