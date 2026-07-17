import { useState } from "react";
import EquityChart from "./EquityChart";

const SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF", "USDCAD"];

export default function BacktestPanel({ onRun }) {
  const [form, setForm] = useState({
    symbol: "EURUSD",
    period: "60d",
    interval: "15m",
    starting_balance: 10000,
    risk_percent: 1,
    stop_loss_pips: 20,
    take_profit_pips: 40,
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
        <button className="btn btn-primary" type="submit" disabled={loading}>
          {loading ? "Running..." : "Run Backtest"}
        </button>
      </form>

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
          </div>
          <EquityChart data={result.equity_curve} title="Backtest Equity Curve" />
        </div>
      )}
    </div>
  );
}
