# Gold Grid

The original fixed grid strategy is restored, with immediate grid replacement
after a profitable basket and Pakistan time throughout the dashboard.

## Trading rules

| Setting | Restored value |
| --- | --- |
| Buy stops | 10 |
| Sell stops | 10 |
| Lot per order | 0.01 |
| Grid distance | 0.30 price units |
| Combined basket profit target | 10.00 USD |
| Indicators or AI entry filter | None |
| Individual order SL/TP | None |

After the basket reaches its target, the bot cancels old pending orders, closes
its positions, verifies that both are gone and builds the replacement grid in
the same engine tick. It does not deliberately wait for the next M1 candle.
Broker execution can still take time or cross a candle boundary.

Daily targets, the configured session and existing risk limits still apply.
If closure or cancellation fails, the old basket is retried before replacement.
If settlement is pending, replacement waits for the verified result without
requiring a new candle once that result arrives.

Initial Start, manual deletion of all grid orders and basket stop loss exits
retain the original next candle behavior. Stop Bot pauses the original trading
loop; it does not close positions or cancel broker orders.

The distance between adjacent levels is 0.30. The first stop on each side also
respects the original broker distance buffer, so it can be farther from the
market when the broker's minimum distance or spread requires it.

## Existing settings and history

On the first backend launch after this update, the five grid values in the
table above are restored once. Existing settings are copied to
backend/runtime_settings.before_grid_restore.json before this change.
Your broker symbol, account connection, polling interval, daily target and
risk amounts keep their saved values. Future settings edits persist normally.

The original default polling interval is 5 seconds. An existing .env or saved
runtime setting can override it, as before. Grid restart after profit does not
add a polling delay or a candle delay once closure completes.

Database compatibility, account ownership and duplicate protection remain so
installations that ran the previous update can keep their history. No database
reset is required. The previous added AI gate, extra entry filters and individual
disaster stop are removed from active trading.

## Pakistan time and interface

Trade opening and closing times, chart crosshair, chart time axis and history
tooltips display Asia/Karachi (PKT, UTC+5), regardless of the computer timezone.
UTC timestamps from the database are explicitly identified before conversion.
The UI includes a basket progress panel, visible lot/distance/level settings,
clearer account cards and a responsive trade table. Manual connection testing
is available in the expandable section below trade history.

Session start/end settings remain UTC and are labelled UTC. The time display
change does not silently shift the trading session.

## Update on Windows PowerShell

Before updating, finish the current bot basket and verify its positions and
pending orders in MT5. Stop the backend, then back up .env, runtime_settings.json
and trading_bot.db. Do not delete your database or broker credentials.

From the repository root:

```powershell
git status
git config pull.ff only
git pull origin HEAD
cd backend
.\venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-mt5.txt
.\venv\Scripts\python.exe run.py
```

From another terminal in the repository root:

```powershell
cd frontend
npm ci
npm run dev
```

If Git reports a conflict, preserve local changes and resolve the conflict before
starting the backend. Do not use a hard reset. After startup, Settings should show
0.01 lots, 10 buy levels, 10 sell levels, 0.30 distance and a $10 basket target.

## Checks

```powershell
cd backend
.\venv\Scripts\python.exe -m pytest -q
cd ../frontend
npm test
npm run lint
npm run build
```

The tests cover same candle profit replacement, duplicate prevention, failed
close/cancel retries, daily target blocking, unchanged loss/startup candle gates,
fixed lot/spacing, MT5 orders without individual SL/TP, settings migration,
existing database compatibility and PKT formatting across computer timezones.

The candle backtest also permits profit cycles within one assumed candle path.
It remains an approximation with proxy data and cannot verify broker execution
latency or actual profitability.
