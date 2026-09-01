import { useState } from "react";
import EquityChart from "./EquityChart";

export default function BacktestPanel({ onRun }) {
  const [form, setForm] = useState({
    symbol: "XAUUSD",
    period: "7d",
    interval: "1m",
    starting_balance: 10000,
    lot_size: 0.01,
    buy_stop_levels: 10,
    sell_stop_levels: 10,
    grid_distance: 0.3,
    basket_take_profit_usd: 10,
    basket_stop_loss_usd: 0,
    spread_points: 24,
    max_daily_loss_usd: 100,
    max_equity_drawdown_percent: 30,
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
        symbol: form.symbol,
        period: form.period,
        interval: form.interval,
        starting_balance: Number(form.starting_balance),
        lot_size: Number(form.lot_size),
        buy_stop_levels: Number(form.buy_stop_levels),
        sell_stop_levels: Number(form.sell_stop_levels),
        grid_distance: Number(form.grid_distance),
        basket_take_profit_usd: Number(form.basket_take_profit_usd),
        basket_stop_loss_usd: Number(form.basket_stop_loss_usd),
        spread_points: Number(form.spread_points),
        max_daily_loss_usd: Number(form.max_daily_loss_usd),
        max_equity_drawdown_percent: Number(form.max_equity_drawdown_percent),
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
      <p className="muted">
        Replays the same grid cycle on past candles — build, fill, close the basket at the target, rebuild — so the
        strategy can be judged before money is on it. Use the <b>1 min</b> interval: the grid is an M1 strategy, and
        a coarser bar hides the order in which levels were reached. <b>Spread</b> is charged on every fill; leaving
        it at 0 makes any grid result fiction, because the grid pays it more often than anything else does.
      </p>
      <form className="settings-form" onSubmit={handleRun}>
        <label>
          Symbol
          <input value={form.symbol} onChange={(e) => update("symbol", e.target.value)} />
        </label>
        <label>
          Period
          <select value={form.period} onChange={(e) => update("period", e.target.value)}>
            <option value="7d">7 days</option>
            <option value="30d">30 days</option>
            <option value="60d">60 days</option>
          </select>
        </label>
        <label>
          Interval
          <select value={form.interval} onChange={(e) => update("interval", e.target.value)}>
            <option value="1m">1 min (7 days max)</option>
            <option value="5m">5 min (hides fill order)</option>
          </select>
        </label>
        <label>
          Starting Balance
          <input
            type="number"
            value={form.starting_balance}
            onChange={(e) => update("starting_balance", e.target.value)}
          />
        </label>
        <label>
          Lot size
          <input type="number" step="0.01" value={form.lot_size} onChange={(e) => update("lot_size", e.target.value)} />
        </label>
        <label>
          Buy stop levels
          <input
            type="number"
            value={form.buy_stop_levels}
            onChange={(e) => update("buy_stop_levels", e.target.value)}
          />
        </label>
        <label>
          Sell stop levels
          <input
            type="number"
            value={form.sell_stop_levels}
            onChange={(e) => update("sell_stop_levels", e.target.value)}
          />
        </label>
        <label>
          Grid distance
          <input
            type="number"
            step="0.01"
            value={form.grid_distance}
            onChange={(e) => update("grid_distance", e.target.value)}
          />
        </label>
        <label>
          Basket take profit ($)
          <input
            type="number"
            step="0.5"
            value={form.basket_take_profit_usd}
            onChange={(e) => update("basket_take_profit_usd", e.target.value)}
          />
        </label>
        <label>
          Basket stop loss ($, 0 = off)
          <input
            type="number"
            step="1"
            value={form.basket_stop_loss_usd}
            onChange={(e) => update("basket_stop_loss_usd", e.target.value)}
          />
        </label>
        <label>
          Spread (points, per fill)
          <input type="number" value={form.spread_points} onChange={(e) => update("spread_points", e.target.value)} />
        </label>
        <label>
          Max daily loss ($)
          <input
            type="number"
            value={form.max_daily_loss_usd}
            onChange={(e) => update("max_daily_loss_usd", e.target.value)}
          />
        </label>
        <label>
          Max equity drawdown (%)
          <input
            type="number"
            value={form.max_equity_drawdown_percent}
            onChange={(e) => update("max_equity_drawdown_percent", e.target.value)}
          />
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
              <div className="card-label">Baskets</div>
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
              <div className="card-label">Worst Basket</div>
              <div className="card-value tone-red">${result.worst_trade ?? "—"}</div>
            </div>
            <div className="card">
              <div className="card-label">Most Positions At Once</div>
              <div className="card-value">{result.max_positions_open ?? "—"}</div>
            </div>
          </div>
          <p className="muted">
            Read <b>Worst Basket</b> and <b>Max Drawdown</b> first, before Win Rate. This strategy closes at a fixed
            profit and has no fixed loss, so a high win rate is built into its design and tells you nothing on its
            own — every grid produces one. The question is what the losing baskets cost when they come.{" "}
            <b>Most Positions At Once</b> is how much exposure the grid actually built up: multiply it by the lot
            size to see the position you were really carrying.
          </p>
          <EquityChart data={result.equity_curve} title="Backtest Equity Curve" />
        </div>
      )}
    </div>
  );
}
