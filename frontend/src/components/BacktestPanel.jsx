import { useState } from "react";
import EquityChart from "./EquityChart";

const SYMBOLS = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF", "USDCAD"];

export default function BacktestPanel({ onRun }) {
  const [form, setForm] = useState({
    symbol: "XAUUSD",
    period: "60d",
    interval: "5m",
    starting_balance: 10000,
    risk_percent: 1,
    stop_loss_pips: 100,
    take_profit_pips: 200,
    strategy: "momentum_scalp",
    sensitivity: "balanced",
    spread_points: 24,
    fixed_lot_size: 0,
    basket_mode: false,
    basket_max_entries: 5,
    basket_add_gap_points: 50,
    basket_target_usd: 3,
    basket_max_loss_usd: 15,
  });
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  function update(key, value) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  async function handleRun(e) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const data = await onRun({
        ...form,
        starting_balance: Number(form.starting_balance),
        risk_percent: Number(form.risk_percent),
        stop_loss_pips: Number(form.stop_loss_pips),
        take_profit_pips: Number(form.take_profit_pips),
        spread_points: Number(form.spread_points),
        fixed_lot_size: Number(form.fixed_lot_size),
        basket_mode: Boolean(form.basket_mode),
        basket_max_entries: Number(form.basket_max_entries),
        basket_add_gap_points: Number(form.basket_add_gap_points),
        basket_target_usd: Number(form.basket_target_usd),
        basket_max_loss_usd: Number(form.basket_max_loss_usd),
      });
      setResult(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="panel">
      <h3>Backtest (historical data)</h3>
      <form className="settings-form" onSubmit={handleRun}>
        <label>
          Symbol
          <select value={form.symbol} onChange={(e) => update("symbol", e.target.value)}>
            {SYMBOLS.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <label>
          Strategy
          <select value={form.strategy} onChange={(e) => update("strategy", e.target.value)}>
            <option value="momentum_scalp">Momentum Scalp (fastest)</option>
            <option value="retest_rejection">M5 Breakout → M1 Retest Rejection</option>
            <option value="scalp_breakout">Scalping — Breakout + Volume</option>
            <option value="ema_rsi">EMA Crossover + RSI</option>
          </select>
        </label>
        <label>
          Sensitivity
          <select value={form.sensitivity} onChange={(e) => update("sensitivity", e.target.value)}>
            <option value="aggressive">Aggressive</option>
            <option value="balanced">Balanced</option>
            <option value="conservative">Conservative</option>
          </select>
        </label>
        <label>
          Period
          <select value={form.period} onChange={(e) => update("period", e.target.value)}>
            <option value="7d">7 days</option>
            <option value="30d">30 days</option>
            <option value="60d">60 days</option>
            <option value="1y">1 year</option>
          </select>
        </label>
        <label>
          Interval
          <select value={form.interval} onChange={(e) => update("interval", e.target.value)}>
            <option value="1m">1 min (7 days max)</option>
            <option value="5m">5 min</option>
            <option value="15m">15 min</option>
            <option value="1h">1 hour</option>
            <option value="1d">1 day</option>
          </select>
        </label>
        <label>
          Starting Balance
          <input type="number" value={form.starting_balance} onChange={(e) => update("starting_balance", e.target.value)} />
        </label>
        <label>
          Risk %
          <input type="number" step="0.1" value={form.risk_percent} onChange={(e) => update("risk_percent", e.target.value)} />
        </label>
        <label>
          Stop Loss (pips)
          <input type="number" value={form.stop_loss_pips} onChange={(e) => update("stop_loss_pips", e.target.value)} />
        </label>
        <label>
          Take Profit (pips)
          <input type="number" value={form.take_profit_pips} onChange={(e) => update("take_profit_pips", e.target.value)} />
        </label>
        <label>
          Spread (points, charged per entry)
          <input type="number" value={form.spread_points} onChange={(e) => update("spread_points", e.target.value)} />
        </label>
        <label>
          Fixed Lot (0 = from risk %)
          <input
            type="number"
            step="0.01"
            min="0"
            value={form.fixed_lot_size}
            onChange={(e) => update("fixed_lot_size", e.target.value)}
          />
        </label>
        <label className="settings-span">
          <input
            type="checkbox"
            checked={Boolean(form.basket_mode)}
            onChange={(e) => update("basket_mode", e.target.checked)}
          />{" "}
          Test basket mode (multi-entry, exited as a group)
        </label>
        <label>
          Basket: max entries
          <input
            type="number"
            min="1"
            disabled={!form.basket_mode}
            value={form.basket_max_entries}
            onChange={(e) => update("basket_max_entries", e.target.value)}
          />
        </label>
        <label>
          Basket: gap before adding (points)
          <input
            type="number"
            min="1"
            disabled={!form.basket_mode}
            value={form.basket_add_gap_points}
            onChange={(e) => update("basket_add_gap_points", e.target.value)}
          />
        </label>
        <label>
          Basket: group target $
          <input
            type="number"
            step="0.5"
            disabled={!form.basket_mode}
            value={form.basket_target_usd}
            onChange={(e) => update("basket_target_usd", e.target.value)}
          />
        </label>
        <label>
          Basket: group stop $
          <input
            type="number"
            step="1"
            disabled={!form.basket_mode}
            value={form.basket_max_loss_usd}
            onChange={(e) => update("basket_max_loss_usd", e.target.value)}
          />
        </label>
        <button className="btn btn-primary" type="submit" disabled={loading}>
          {loading ? "Running..." : "Run Backtest"}
        </button>
      </form>
      <p className="muted">
        Spread is charged on every entry, so leaving it at 0 will make any fast strategy look far better than it can
        be. On gold it is normally 20–30 points. This matters most in basket mode: the group target is a fixed dollar
        amount, so more entries means a smaller price move is needed to reach it — while the spread paid to open
        those entries keeps adding up.
      </p>

      {error && <p className="error-text">⚠ {error}</p>}

      {result && (
        <div className="backtest-result">
          <div className="stat-grid">
            <div className="card">
              <div className="card-label">Total Trades</div>
              <div className="card-value">{result.total_trades}</div>
            </div>
            <div className="card">
              <div className="card-label">Win Rate</div>
              <div className="card-value">{result.win_rate}%</div>
            </div>
            <div className="card">
              <div className="card-label">Total Profit</div>
              <div className={`card-value ${result.total_profit >= 0 ? "tone-green" : "tone-red"}`}>
                ${result.total_profit}
              </div>
            </div>
            <div className="card">
              <div className="card-label">Max Drawdown</div>
              <div className="card-value tone-red">{result.max_drawdown_percent}%</div>
            </div>
            <div className="card">
              <div className="card-label">Profit Factor</div>
              <div className="card-value">{result.profit_factor ?? "—"}</div>
            </div>
            <div className="card">
              <div className="card-label">Ending Balance</div>
              <div className="card-value">${result.ending_balance}</div>
            </div>
            <div className="card">
              <div className="card-label">Worst Single Trade</div>
              <div className="card-value tone-red">${result.worst_trade ?? "—"}</div>
            </div>
            <div className="card">
              <div className="card-label">Best Single Trade</div>
              <div className="card-value tone-green">${result.best_trade ?? "—"}</div>
            </div>
          </div>
          <p className="muted">
            Read <b>Worst Single Trade</b> next to Win Rate, not on its own. A high win rate with one very large loss
            is the signature of a system that pays out small and often and takes it all back at once — that shape can
            look excellent for weeks before it doesn't.
          </p>
          <EquityChart data={result.equity_curve} title="Backtest Equity Curve" />
        </div>
      )}
    </div>
  );
}
