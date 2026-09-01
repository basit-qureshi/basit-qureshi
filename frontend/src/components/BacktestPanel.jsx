import { useState } from "react";
import EquityChart from "./EquityChart";

const SYMBOLS = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF", "USDCAD"];

export default function BacktestPanel({ onRun }) {
  const [form, setForm] = useState({
    symbol: "XAUUSD",
    period: "7d",
    interval: "1m",
    starting_balance: 10000,
    risk_percent: 1,
    strategy: "smc",
    sensitivity: "balanced",
    spread_points: 24,
    fixed_lot_size: 0,
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
        spread_points: Number(form.spread_points),
        fixed_lot_size: Number(form.fixed_lot_size),
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
            <option value="smc">Smart Money Concepts (M15 → M1 → M5)</option>
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
            <option value="5m">5 min (too coarse for SMC)</option>
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
        <button className="btn btn-primary" type="submit" disabled={loading}>
          {loading ? "Running..." : "Run Backtest"}
        </button>
      </form>
      <p className="muted">
        SMC reads M15 and M5 structure by resampling the data you pick here, so use the <b>1 min</b> interval —
        anything coarser leaves nothing to build M1 structure from. Spread is charged on every entry; leaving it at 0
        makes any result look better than it can be. On gold it is normally 20–30 points.
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
          {result.stage_breakdown?.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Where the strategy stopped</th>
                    <th>Checks</th>
                    <th>Share</th>
                  </tr>
                </thead>
                <tbody>
                  {result.stage_breakdown.map((row) => (
                    <tr key={row.stage}>
                      <td>{row.stage}</td>
                      <td>{row.count}</td>
                      <td>{row.percent}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {result.setup_funnel && (
            <p className="muted">
              <b>Setups:</b> {result.setup_funnel.armed} found · {result.setup_funnel.filled} filled ·{" "}
              {result.setup_funnel.expired} expired unfilled · {result.setup_funnel.invalidated} invalidated when
              price broke the zone. If very few are <i>found</i>, the structure filters are too tight for this data —
              loosen Sensitivity or lower Min RR. If many are found but few <i>fill</i>, the entries are the problem —
              lower Fallback distance so the bot takes more of them at market.
            </p>
          )}
          {result.entry_breakdown && (
            <p className="muted">
              <b>Entries:</b> {result.entry_breakdown.tap.count} on a zone tap (${result.entry_breakdown.tap.profit}),{" "}
              {result.entry_breakdown.fallback.count} taken at market after price ran away without tapping ($
              {result.entry_breakdown.fallback.profit}). The second number tells you whether the fallback rule is
              earning its place — if it is consistently negative, raise <i>Fallback min RR</i> or turn it off by
              setting <i>Fallback distance</i> very high.
            </p>
          )}
          <EquityChart data={result.equity_curve} title="Backtest Equity Curve" />
        </div>
      )}
    </div>
  );
}
