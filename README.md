# Forex AI Trading Bot — XAUUSD M1 Pending-Order Grid

An automated gold trading bot: connects to a broker (Exness via MetaTrader5, or a
built-in simulated broker for testing), runs a pending-order grid, and — only
within risk limits you set — places, manages and closes trades automatically. A
React dashboard shows a live candlestick chart, the grid's state, split daily
profit and loss figures, open positions, trade history and an equity curve, and
lets you backtest the strategy on historical data before risking money.

## The strategy

One strategy, on XAUUSD M1, and nothing else:

1. Place `GRID_BUY_STOP_LEVELS` BUY STOPs above the market and
   `GRID_SELL_STOP_LEVELS` SELL STOPs below it, every order at the same fixed
   `GRID_LOT_SIZE`, spaced by `GRID_DISTANCE`.
2. Wait. Stops become positions as price reaches them.
3. Add up the **combined** net profit of every position this bot opened. No
   individual trade has to reach the target.
4. The moment that total reaches `GRID_BASKET_TAKE_PROFIT_USD`, close every
   position, cancel every remaining pending order, and verify nothing survived.
5. Build a fresh grid around the current price — **on the next M1 candle**, never
   the one the profit was booked on.
6. Repeat.

There are no indicators, signals, filters, trend rules, martingale, lot scaling,
recovery logic, trailing logic, or per-trade stop loss and take profit. A grid
position is closed only by the basket rule or by a risk limit.

Every order carries `GRID_MAGIC_NUMBER`. Manual trades and orders from any other
program are never counted, modified, cancelled or closed.

### The next-M1-candle rule

A fresh grid is never placed on the same M1 candle the engine became ready to
build on. That applies to all of:

- Start Bot with no existing positions and no existing pending orders
- a basket closing at its take profit
- a basket closing on its basket stop or a risk protection
- all pending orders disappearing (deleted by hand in MT5, or expired) while no
  positions are open
- a new broker trading day beginning after a daily-target halt

The engine stores the current broker M1 candle stamp as an anchor, places nothing
while the broker still reports that candle, and builds exactly one grid once a
strictly later candle is confirmed. **If candle data is unavailable it places
nothing at all** — the rule is kept from confirmed broker data, never from the
machine clock.

Starting the bot when it already has positions or resting orders does not
disturb them; the delay only applies when a fresh grid is needed. A partially
filled or partially deleted grid is never topped up.

### Daily profit target

`GRID_DAILY_PROFIT_TARGET_USD` stops trading for the rest of the broker day once
that much **net realized** profit has been booked. It is `0` (off) by default.

- Realized means settled trades after their losses, commission and swap — never
  floating profit, equity movement, deposits, or gross winners.
- Scoped to this bot's identity: its symbol, account mode and magic number. A
  manual trade or a Manual Test Trade cannot move it.
- Judged at currency precision: with a $10.00 target, $9.99 does not halt and
  $10.00 does.
- On reaching it, every pending order is cancelled and verified gone, and no new
  order is placed for the rest of that broker day.
- The lock is **derived from the settled trades**, not held in memory, so
  restarting the backend, refreshing the browser or pressing Start again cannot
  bypass it. A new broker day clears it, and the first grid of the new day still
  waits for the next confirmed candle.
- A trade whose realized result the broker has not reported yet is counted as
  unsettled, and a new grid is held back until the day's total is certain.

### Daily figures

The dashboard shows three separate numbers, all from the same accounting source
the daily halt is judged on:

| Card | Meaning |
| --- | --- |
| Today's Profit | sum of the positive realized results closed today |
| Today's Loss | absolute sum of the negative realized results closed today |
| Today's Net P&L | profit minus loss |

For results of +$15.00, −$4.00 and −$6.00 that reads $15.00, $10.00 and $5.00 —
and a $10.00 daily target does **not** trigger, because the net is $5.00.

The broker trading day comes from the broker's own M1 candle stamps, interpreted
in `TIMEZONE` (default `Asia/Karachi`), so the engine, dashboard and backtester
all divide days the same way. All times on screen are shown in that zone.

## Risk

Read this before running it with money.

There is no stop loss on an individual grid trade, so a basket that never reaches
its target keeps growing while price runs. At 0.01 lots a fully triggered 10+10
grid is 0.20 lots, and on gold that is **$20 of profit or loss for every $1 the
price moves**.

There is also a dead end built into a two-sided grid. Once both sides have
filled, the buys and sells net to zero, the price terms cancel, and the basket's
profit **stops responding to price**. It freezes at the sell entries minus the
buy entries, less the spread paid to open them — always a loss, because the buys
filled above the reference and the sells below it. A full 10+10 grid at 0.30
spacing locks in about **−$37.80 at every price**, and the take profit cannot be
reached from there by any means. The engine detects this and says so on the
dashboard rather than looking stalled.

`GRID_BASKET_STOP_LOSS_USD`, `GRID_MAX_DAILY_LOSS_USD`,
`GRID_MAX_EQUITY_DRAWDOWN_PERCENT` and `GRID_MAX_OPEN_POSITIONS` are therefore
the entire risk model. If they do not end a basket, nothing will.

## Architecture

```
backend/            FastAPI app — the trading engine
  app/brokers/       mock_broker.py (simulated, runs anywhere)
                     mt5_broker.py (real Exness/MT5 connection, Windows only)
                     Both support market orders and BUY STOP / SELL STOP pendings
  app/engine/        grid_engine.py — the grid cycle, candle gate and daily target
  app/backtest/      Replays the same cycle on historical M1 candles
  app/db.py          SQLite trade history and the shared daily accounting source
  app/api/           REST + WebSocket endpoints
  tests/             pytest suite
frontend/           React + Vite dashboard
```

## Running it

```bash
# backend
cd backend
python -m venv venv && source venv/bin/activate   # Windows: .\venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r requirements-mt5.txt               # Windows only, for a real MT5 connection
cp .env.example .env                              # then fill in your MT5 details
uvicorn app.main:app --reload

# frontend, in a second terminal
cd frontend
npm install
npm run dev
```

Open http://localhost:5173. For a real connection, MetaTrader 5 must be running,
logged in, with **Algo Trading** enabled.

## Tests

```bash
cd backend && python -m pytest -q     # engine, gate, daily target, API, migration, backtest parity
cd frontend && npm run lint && npm run build
```

## Database

Trade history lives in `backend/trading_bot.db`. The schema gains columns in
place on startup when needed; an existing database keeps all of its rows.
