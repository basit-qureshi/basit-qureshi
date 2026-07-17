# Forex AI Trading Bot

An automated forex trading bot: connects to a broker (Exness via MetaTrader5, or a
built-in simulated broker for testing), watches the market with a rule-based
strategy, and — only within risk limits you set — places, manages, and closes
trades automatically. A React dashboard shows live stats, open positions, trade
history, and an equity curve, and lets you backtest the strategy on historical
data before ever risking money.

## Architecture

```
backend/            FastAPI app — the trading engine
  app/brokers/       Broker abstraction: mock_broker.py (simulated, runs anywhere)
                      and mt5_broker.py (real Exness/MT5 connection, Windows only)
  app/strategy/       EMA crossover + RSI filter signal generator
  app/risk/           Position sizing, stop loss/take profit, max daily loss, max open trades
  app/engine/         Ties broker + strategy + risk together in a poll loop
  app/backtest/       Runs the strategy over historical data (via yfinance)
  app/api/            REST endpoints + WebSocket for live updates
frontend/            React (Vite) dashboard
```

The strategy can never bypass the risk manager — every signal is checked against
your risk rules before an order is placed. Rule-based (EMA+RSI) is the v1
strategy; it's isolated behind a small interface so a real ML model can replace
or augment it later without touching the risk engine or broker code.

## Two broker modes

- **`mock`** (default): a simulated broker with a fake price feed and fake
  balance. No external account needed, runs on Linux/Mac/Windows/this sandbox.
  Use this to develop, test the dashboard, and sanity-check the bot's behavior.
- **`mt5`**: connects to your real Exness demo or real account through the
  MetaTrader5 terminal. **This only works on Windows** — Exness (like most
  retail forex brokers) has no public REST API; the `MetaTrader5` Python
  package talks to a locally installed, logged-in MT5 terminal over local IPC.
  There is no cloud/Linux equivalent for this step.

## Running locally (mock mode — works right now, any OS)

Backend:
```bash
cd backend
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # BROKER_MODE=mock by default
python run.py                    # http://localhost:8000
```

Frontend (separate terminal):
```bash
cd frontend
npm install
npm run dev                      # http://localhost:5173
```

Open `http://localhost:5173`. Click **Start Bot** — it will trade on the
simulated $10,000 demo account so you can see the whole flow (signals, orders,
stop loss/take profit, stats, equity curve) without any real account.

## Switching to your real Exness account (Windows)

1. Install [MetaTrader 5](https://www.exness.com) and log into your Exness
   **demo** account first (Exness gives you the login/password/server when you
   open a demo account — server is something like `Exness-MT5Trial`).
2. Keep the MT5 terminal open and logged in.
3. In `backend/.env`:
   ```
   BROKER_MODE=mt5
   MT5_LOGIN=12345678
   MT5_PASSWORD=your-mt5-password
   MT5_SERVER=Exness-MT5Trial
   ACCOUNT_TYPE=demo
   ```
4. `pip install -r requirements.txt` again on Windows (this installs the
   Windows-only `MetaTrader5` package this time).
5. Restart the backend. The dashboard's connection dot should turn green and
   show your real Exness demo balance.
6. Only after you've watched it trade correctly on demo for a while, switch
   `ACCOUNT_TYPE=real` (or use the dashboard's mode switch) to go live. Both the
   mode switch and the Start button require an explicit confirmation when
   switching to/starting on a real-money account — there is no accidental way
   to trade real money.

## Risk controls (Settings tab)

- **Risk per trade (%)** — how much of your balance is risked on each trade;
  position size (lot size) is calculated from this and your stop loss distance.
- **Stop Loss / Take Profit (pips)** — every trade always has both.
- **Max Open Trades** — caps concurrent positions.
- **Max Daily Loss (%)** — if the account's equity drops this much below where
  it started the day, the bot stops opening new trades for the rest of the day.

These are enforced in `app/risk/risk_manager.py` and cannot be bypassed by the
strategy — a BUY/SELL signal only reaches the broker if the risk manager
approves it.

## Backtesting

The **Backtest** tab downloads historical forex candles (via `yfinance`) and
replays the exact same strategy + risk manager logic bar-by-bar, reporting win
rate, total profit, max drawdown, and profit factor, with an equity curve.
Always backtest a change before running it live.

## Roadmap (agreed plan)

1. ✅ Rule-based strategy (EMA crossover + RSI filter) + full risk engine + demo
   trading + backtester + dashboard — this is what's built.
2. Swap in real Exness MT5 connection (above), validate on **demo** for a
   meaningful stretch before touching a real account.
3. Add an ML signal model (e.g. a classifier trained on engineered features
   from price history) behind the same `generate_signal()` interface used by
   `EmaRsiStrategy`, so it plugs into the existing engine/risk/dashboard
   unchanged.
4. Multi-symbol support, more strategies, alerting (Telegram/email on trades).

## Known limitations (MVP, by design)

- Only one strategy (EMA+RSI) and one broker pair type (MT5) are wired up.
- The mock broker's price feed is a random walk — good for testing plumbing,
  not for judging real profitability. Use the backtester (real historical
  data) for that.
- No user accounts/auth yet — this runs as a single-user local tool.
