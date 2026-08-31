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
      strategy: form.strategy,
      sensitivity: form.sensitivity,
      risk_percent: Number(form.risk_percent),
      fixed_lot_size: Number(form.fixed_lot_size),
      max_open_trades: Number(form.max_open_trades),
      max_daily_loss_percent: Number(form.max_daily_loss_percent),
      max_daily_trades: Number(form.max_daily_trades),
      max_spread_points: Number(form.max_spread_points),
      poll_interval_seconds: Number(form.poll_interval_seconds),
      smc_sl_buffer_points: Number(form.smc_sl_buffer_points),
      smc_min_rr: Number(form.smc_min_rr),
      smc_fallback_points: Number(form.smc_fallback_points),
      smc_fallback_min_rr: Number(form.smc_fallback_min_rr),
      smc_setup_expiry_minutes: Number(form.smc_setup_expiry_minutes),
      smc_mss_max_age: Number(form.smc_mss_max_age),
      smc_zone_tolerance_points: Number(form.smc_zone_tolerance_points),
    });
    if (ok) setDirty(false); // keep unsaved edits on screen if the save was rejected
  }

  return (
    <div className="panel">
      <h3>Strategy &amp; Risk Settings</h3>
      <p className="muted">
        The bot runs one strategy: <b>Smart Money Concepts</b>. It reads structure on <b>M15</b> (break of structure,
        then the order block behind it — which only counts if it left a fair value gap, and only if it sits in
        discount for a buy or premium for a sell), waits for price to return to that zone, requires an <b>M1</b>{" "}
        market structure shift to confirm the pullback is done, and then places the entry on the <b>M5</b> zone.
        Stop and target come from the structure itself, not from a fixed pip setting — so there is no Stop Loss or
        Take Profit box here to set.
      </p>
      <p className="muted">
        <b>Missed entries.</b> If price never comes back to tap the M5 zone and instead runs <i>Fallback distance</i>{" "}
        past the M1 shift, the bot enters at market rather than watching the move go without it — but only while at
        least <i>Fallback min RR</i> is still left between the current price and the target. That is the trade-off:
        you stop missing moves, and you pay for it with a worse entry price on those trades.
      </p>
      <p className="muted">
        <b>Stop placement.</b> The stop sits beyond the M5 zone by the live spread plus <i>SL buffer</i>. A stop
        resting exactly on the zone's edge gets taken out by the spread and one wick before the move it was placed
        for — on gold the spread alone is 24–30 points, so a buffer is not optional.
      </p>
      <p className="muted">
        <b>Stop trailing</b> follows the reward:risk ladder automatically: at 1:1 the stop moves to breakeven, at
        1:2 it moves to 1:1, at 1:3 to 1:2, and so on for as long as the trade keeps running.
      </p>
      <p className="muted">
        <b>If the bot trades too rarely</b>, these two are the knobs that change how often a setup appears at all.{" "}
        <i>MSS still counts for</i> is how long after an M1 structure shift the shift is still treated as valid —
        price can sit inside an order block for a while before it turns, and too short a window throws those setups
        away before the turn happens. <i>Zone tolerance</i> is how close to the M15 order block counts as reaching
        it. Widen them for more setups; each one will be a little looser than the last.
      </p>
      <p className="muted">
        <b>Min RR</b> is the filter that decides whether a setup is worth taking at all. The target is wherever the
        break of structure ran to, so when that target is too close to the entry relative to the stop, the setup is
        skipped rather than taken at a poor ratio.
      </p>
      {running && <p className="muted">Stop the bot to change settings.</p>}
      <form className="settings-form" onSubmit={handleSubmit}>
        <label>
          Symbol (type your broker's exact name, e.g. XAUUSDm)
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
          Chart timeframe (display only)
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
            <option value="smc">Smart Money Concepts (M15 → M1 → M5)</option>
          </select>
        </label>
        <label>
          Sensitivity (how much structure counts)
          <select disabled={running} value={form.sensitivity} onChange={(e) => update("sensitivity", e.target.value)}>
            <option value="aggressive">Aggressive — small swings count, more setups</option>
            <option value="balanced">Balanced</option>
            <option value="conservative">Conservative — only clear structure, fewest setups</option>
          </select>
        </label>

        <label className="settings-span">Entry &amp; stop placement</label>
        <label>
          Min RR to take a setup
          <input
            disabled={running}
            type="number"
            step="0.5"
            min="0.5"
            value={form.smc_min_rr}
            onChange={(e) => update("smc_min_rr", e.target.value)}
          />
        </label>
        <label>
          SL buffer beyond the zone (points)
          <input
            disabled={running}
            type="number"
            min="0"
            value={form.smc_sl_buffer_points}
            onChange={(e) => update("smc_sl_buffer_points", e.target.value)}
          />
        </label>
        <label>
          Fallback distance (points)
          <input
            disabled={running}
            type="number"
            min="0"
            value={form.smc_fallback_points}
            onChange={(e) => update("smc_fallback_points", e.target.value)}
          />
        </label>
        <label>
          Fallback min RR
          <input
            disabled={running}
            type="number"
            step="0.5"
            min="0"
            value={form.smc_fallback_min_rr}
            onChange={(e) => update("smc_fallback_min_rr", e.target.value)}
          />
        </label>
        <label>
          Drop unfilled setup after (minutes)
          <input
            disabled={running}
            type="number"
            min="0"
            value={form.smc_setup_expiry_minutes}
            onChange={(e) => update("smc_setup_expiry_minutes", e.target.value)}
          />
        </label>

        <label className="settings-span">How often setups appear</label>
        <label>
          MSS still counts for (M1 bars)
          <input
            disabled={running}
            type="number"
            min="1"
            value={form.smc_mss_max_age}
            onChange={(e) => update("smc_mss_max_age", e.target.value)}
          />
        </label>
        <label>
          Zone tolerance (points)
          <input
            disabled={running}
            type="number"
            min="0"
            value={form.smc_zone_tolerance_points}
            onChange={(e) => update("smc_zone_tolerance_points", e.target.value)}
          />
        </label>

        <label className="settings-span">Risk</label>
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
          Fixed Lot Size (0 = auto from risk %)
          <input
            disabled={running}
            type="number"
            step="0.01"
            min="0"
            value={form.fixed_lot_size}
            onChange={(e) => update("fixed_lot_size", e.target.value)}
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
          Max Spread (points, 0 = off)
          <input
            disabled={running}
            type="number"
            min="0"
            value={form.max_spread_points}
            onChange={(e) => update("max_spread_points", e.target.value)}
          />
        </label>
        <label>
          Poll Interval (seconds)
          <input
            disabled={running}
            type="number"
            min="1"
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
