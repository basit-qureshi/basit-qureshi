import { useEffect, useState } from "react";

const SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF", "USDCAD"];
const TIMEFRAMES = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"];

export default function SettingsPanel({ settings, running, onSave, saving }) {
  const [form, setForm] = useState(settings || {});

  useEffect(() => {
    if (settings) setForm(settings);
  }, [settings]);

  if (!form) return null;

  function update(key, value) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  function handleSubmit(e) {
    e.preventDefault();
    onSave({
      symbol: form.symbol,
      timeframe: form.timeframe,
      risk_percent: Number(form.risk_percent),
      stop_loss_pips: Number(form.stop_loss_pips),
      take_profit_pips: Number(form.take_profit_pips),
      max_open_trades: Number(form.max_open_trades),
      max_daily_loss_percent: Number(form.max_daily_loss_percent),
      poll_interval_seconds: Number(form.poll_interval_seconds),
    });
  }

  return (
    <div className="panel">
      <h3>Strategy &amp; Risk Settings</h3>
      {running && <p className="muted">Stop the bot to change settings.</p>}
      <form className="settings-form" onSubmit={handleSubmit}>
        <label>
          Symbol
          <select disabled={running} value={form.symbol} onChange={(e) => update("symbol", e.target.value)}>
            {SYMBOLS.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <label>
          Timeframe
          <select disabled={running} value={form.timeframe} onChange={(e) => update("timeframe", e.target.value)}>
            {TIMEFRAMES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <label>
          Risk per trade (% of balance)
          <input
            disabled={running}
            type="number"
            step="0.1"
            min="0.1"
            max="10"
            value={form.risk_percent}
            onChange={(e) => update("risk_percent", e.target.value)}
          />
        </label>
        <label>
          Stop Loss (pips)
          <input
            disabled={running}
            type="number"
            min="1"
            value={form.stop_loss_pips}
            onChange={(e) => update("stop_loss_pips", e.target.value)}
          />
        </label>
        <label>
          Take Profit (pips)
          <input
            disabled={running}
            type="number"
            min="1"
            value={form.take_profit_pips}
            onChange={(e) => update("take_profit_pips", e.target.value)}
          />
        </label>
        <label>
          Max Open Trades
          <input
            disabled={running}
            type="number"
            min="1"
            value={form.max_open_trades}
            onChange={(e) => update("max_open_trades", e.target.value)}
          />
        </label>
        <label>
          Max Daily Loss (%)
          <input
            disabled={running}
            type="number"
            step="0.5"
            min="1"
            value={form.max_daily_loss_percent}
            onChange={(e) => update("max_daily_loss_percent", e.target.value)}
          />
        </label>
        <label>
          Poll Interval (seconds)
          <input
            disabled={running}
            type="number"
            min="5"
            value={form.poll_interval_seconds}
            onChange={(e) => update("poll_interval_seconds", e.target.value)}
          />
        </label>
        <button className="btn btn-primary" type="submit" disabled={running || saving}>
          {saving ? "Saving..." : "Save Settings"}
        </button>
      </form>
    </div>
  );
}
