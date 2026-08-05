import { useEffect, useState } from "react";

const SYMBOLS = [
  "XAUUSD",
  "XAUUSDm",
  "EURUSD",
  "GBPUSD",
  "USDJPY",
  "AUDUSD",
  "USDCHF",
  "USDCAD",
  "EURUSDm",
  "GBPUSDm",
  "USDJPYm",
];
const TIMEFRAMES = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"];

export default function SettingsPanel({ settings, running, onSave, saving }) {
  const [form, setForm] = useState(settings || {});
  const [dirty, setDirty] = useState(false);

  // The dashboard polls status every few seconds, handing us a fresh settings
  // object each time. Only sync it into the form while the user has no unsaved
  // edits — otherwise their typing gets wiped mid-edit on every poll.
  useEffect(() => {
    if (settings && !dirty) setForm(settings);
  }, [settings, dirty]);

  if (!form) return null;

  function update(key, value) {
    setDirty(true);
    setForm((f) => ({ ...f, [key]: value }));
  }

  async function handleSubmit(e) {
    e.preventDefault();
    const ok = await onSave({
      symbol: form.symbol,
      timeframe: form.timeframe,
      risk_percent: Number(form.risk_percent),
      stop_loss_pips: Number(form.stop_loss_pips),
      take_profit_pips: Number(form.take_profit_pips),
      max_open_trades: Number(form.max_open_trades),
      max_daily_loss_percent: Number(form.max_daily_loss_percent),
      max_daily_trades: Number(form.max_daily_trades),
      breakeven_trigger_pips: Number(form.breakeven_trigger_pips),
      trailing_stop_pips: Number(form.trailing_stop_pips),
      poll_interval_seconds: Number(form.poll_interval_seconds),
      ema_fast_period: Number(form.ema_fast_period),
      ema_slow_period: Number(form.ema_slow_period),
      strategy: form.strategy,
    });
    if (ok) setDirty(false);  // keep unsaved edits on screen if the save was rejected
  }

  return (
    <div className="panel">
      <h3>Strategy &amp; Risk Settings</h3>
      <p className="muted">
        Breakeven/Trailing: once a trade is this many pips in profit, its stop loss auto-moves to your entry price
        (can't turn into a loss anymore), then keeps trailing behind price by the trailing pips as it keeps moving
        your way — locking in more profit automatically. Leave at 0 to disable.
      </p>
      <p className="muted">
        EMA Fast/Slow: lower numbers make the strategy react faster and trade more often (good for quick
        open-close-profit cycles), at the cost of more false signals/noise trades. Higher numbers trade less often
        but each signal tends to be more reliable.
      </p>
      {running && <p className="muted">Stop the bot to change settings.</p>}
      <form className="settings-form" onSubmit={handleSubmit}>
        <label>
          Symbol (type your broker's exact name, e.g. EURUSDm)
          <input
            disabled={running}
            list="symbol-suggestions"
            type="text"
            value={form.symbol}
            onChange={(e) => update("symbol", e.target.value)}
          />
          <datalist id="symbol-suggestions">
            {SYMBOLS.map((s) => (
              <option key={s} value={s} />
            ))}
          </datalist>
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
          Strategy
          <select disabled={running} value={form.strategy} onChange={(e) => update("strategy", e.target.value)}>
            <option value="scalp_breakout">Scalping — Breakout + Volume (fast, M1/M5)</option>
            <option value="ema_rsi">EMA Crossover + RSI (steady, fewer trades)</option>
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
          Max Daily Trades (0 = unlimited)
          <input
            disabled={running}
            type="number"
            min="0"
            value={form.max_daily_trades}
            onChange={(e) => update("max_daily_trades", e.target.value)}
          />
        </label>
        <label>
          Breakeven Trigger (pips, 0 = off)
          <input
            disabled={running}
            type="number"
            min="0"
            value={form.breakeven_trigger_pips}
            onChange={(e) => update("breakeven_trigger_pips", e.target.value)}
          />
        </label>
        <label>
          Trailing Stop (pips, 0 = breakeven only)
          <input
            disabled={running}
            type="number"
            min="0"
            value={form.trailing_stop_pips}
            onChange={(e) => update("trailing_stop_pips", e.target.value)}
          />
        </label>
        <label>
          EMA Fast Period (lower = faster signals)
          <input
            disabled={running}
            type="number"
            min="2"
            value={form.ema_fast_period}
            onChange={(e) => update("ema_fast_period", e.target.value)}
          />
        </label>
        <label>
          EMA Slow Period (lower = faster signals)
          <input
            disabled={running}
            type="number"
            min="3"
            value={form.ema_slow_period}
            onChange={(e) => update("ema_slow_period", e.target.value)}
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
