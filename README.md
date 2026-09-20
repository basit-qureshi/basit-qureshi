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

Session start/end hours are now read on the same Asia/Karachi clock the rest of
the dashboard uses. They were previously compared against raw UTC while being
presented next to PKT times, so a window set to 17-22 actually opened at 22:00
PKT — five hours later than it read. Anyone who had already set a window should
re-check it: the hours now mean what they say.

## Capital protection

These controls exist to bound a loss, not to find a better entry. None of them
changes the strategy: same levels, same fixed lot, same spacing, same combined
basket target, same magic-number isolation.

| Control | What it does |
| --- | --- |
| Net basket accounting | The target and the stop are judged on profit after swap, commission and an estimated exit cost, not on the broker's gross figure. |
| Retried risk closure | A limit breach keeps trying to close on every poll until the account is actually flat, instead of reporting "halted" once over live positions. |
| Durable halt | A loss halt is written to the database, so restarting the backend or pressing Start does not clear it. Releasing it is an explicit owner action that refuses while any position or pending order is still open. |
| Affordability check | Before placing a grid the bot prices the worst case — the configured basket stop, and the structural loss a fully filled 10+10 grid locks in — and refuses the grid if either does not fit inside the balance less `GRID_CAPITAL_RESERVE_PERCENT`. |
| Loss limits required | With both the basket stop and the daily loss limit at 0 the bot places no new grid and says so. Open positions are still managed and still closed. |

A refusal is always visible on the dashboard with its reason. The bot never
raises risk or lowers a limit on its own to make a grid fit.

### The structural loss the affordability check prices

A two-sided grid has a dead end. Once both sides have filled, the buy and sell
volumes cancel, the price terms drop out, and the basket's profit stops
responding to price at all. It freezes at the sell entries minus the buy
entries, less the spread paid to open them — and because the buys filled above
the reference and the sells below it, that frozen number is always a loss. A
full 10+10 grid at 0.30 spacing locks in roughly -$37.80, and no price in
either direction recovers it. The take profit cannot be reached from there.
`backend/tests/test_capital_protection.py` computes this number rather than
asserting it, so it stays correct if the levels or spacing change.

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

`test_capital_protection.py` and `test_owner_controls.py` add the protections
above: net-versus-gross basket judgement in both directions, a risk close that
is retried until flat, a halt that survives a new engine, an unaffordable grid
refused and an affordable one still placed, entries blocked until loss limits
exist, the trading window read on the Pakistan clock, a halt that cannot be
cleared over live exposure, and a drawdown high-water mark that a new trading
day does not reset. Every one of them was written to fail against the previous
code before the fix was made.

All of it runs against an injected fake broker. No test opens, closes or
modifies anything in a real MT5 terminal.

The candle backtest also permits profit cycles within one assumed candle path.
It remains an approximation with proxy data and cannot verify broker execution
latency or actual profitability.
