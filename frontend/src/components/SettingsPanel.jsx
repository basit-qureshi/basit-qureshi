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
      fixed_lot_size: Number(form.fixed_lot_size),
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
      sensitivity: form.sensitivity,
      max_trade_minutes: Number(form.max_trade_minutes),
      quick_profit_usd: Number(form.quick_profit_usd),
      max_spread_points: Number(form.max_spread_points),
      trend_filter_timeframe: form.trend_filter_timeframe,
      basket_mode: Boolean(form.basket_mode),
      basket_max_entries: Number(form.basket_max_entries),
      basket_add_gap_points: Number(form.basket_add_gap_points),
      basket_target_usd: Number(form.basket_target_usd),
      basket_max_loss_usd: Number(form.basket_max_loss_usd),
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
        but each signal tends to be more reliable. (Not used by the M5→M1 retest strategy, which trades levels
        rather than EMA crosses.)
      </p>
      <p className="muted">
        Speed: <b>Poll Interval</b> is how often the market is checked — on M1 keep it at 5s or the bot only looks
        twice per candle and misses setups. <b>Sensitivity</b> loosens or tightens the entry filters. <b>Auto-close
        after</b> exits a trade that has been sitting open that long without hitting SL or TP, so money isn't parked
        in a setup that stopped working.
      </p>
      <p className="muted">
        <b>Book profit at $</b> closes a trade the moment it's up that much, instead of waiting for Take Profit.
        It books wins sooner, but it caps the winning side while the stop stays where it is — booking $2 against a
        $3 stop is a 1:0.67 ratio, so you'd then need to win about 60% of trades just to break even. Breakeven +
        Trailing above does the "lock in profit" job without shrinking the reward.
      </p>
      <p className="muted">
        Stop Loss / Take Profit are in points — on Gold one point is 0.01, so 100/200 means a $1.00 stop against a
        $2.00 target (1:2). If your broker's minimum stop distance is wider than your stop, both are widened
        together so the ratio stays the same.
      </p>
      <p className="muted">
        <b>Fixed Lot Size</b> trades exactly that many lots every time. Leave it at 0 and the lot is calculated from
        Risk per trade instead, which keeps the money at risk the same on every trade even when the stop distance
        changes. Setting a fixed lot means the dollar risk moves around with the stop — on Gold, 0.01 lots loses
        about $1.00 per 100 points, so a 0.05 lot with a 100-point stop is $5 a trade.
      </p>
      <p className="muted">
        <b>Basket mode</b> is the multi-entry style: instead of one trade with its own take profit, it opens up to
        "max entries" positions in the same direction (a new one only after price has moved the "gap" against the
        last entry) and closes all of them together the moment their <i>combined</i> profit reaches the target.
      </p>
      <p className="muted warn-text">
        ⚠ Read this before switching basket mode on. Adding to a position that is going against you produces a long
        string of small wins followed by one very large loss when price simply keeps going — the small wins are not
        evidence that it works, they are what this shape looks like before the loss arrives. The group stop is the
        only thing that caps that loss, so set it to money you are genuinely willing to lose in one go, and test it
        on demo for a meaningful number of trades before considering real money.
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
            <option value="momentum_scalp">Momentum Scalp — fastest, many trades/hour</option>
            <option value="retest_rejection">M5 Breakout → M1 Retest Rejection (few, selective)</option>
            <option value="scalp_breakout">Scalping — Breakout + Volume (single timeframe)</option>
            <option value="ema_rsi">EMA Crossover + RSI (steady, fewest trades)</option>
          </select>
        </label>
        <label>
          Sensitivity (how often it trades)
          <select disabled={running} value={form.sensitivity} onChange={(e) => update("sensitivity", e.target.value)}>
            <option value="aggressive">Aggressive — many trades, weaker signals</option>
            <option value="balanced">Balanced</option>
            <option value="conservative">Conservative — few trades, cleaner setups</option>
          </select>
        </label>
        <label>
          Trend Filter (only trade with this trend)
          <select
            disabled={running}
            value={form.trend_filter_timeframe}
            onChange={(e) => update("trend_filter_timeframe", e.target.value)}
          >
            <option value="">Off — trade any M1 push</option>
            <option value="M5">M5 trend must agree</option>
            <option value="M15">M15 trend must agree</option>
            <option value="H1">H1 trend must agree</option>
          </select>
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
          Book profit at $ (0 = wait for TP)
          <input
            disabled={running}
            type="number"
            step="0.5"
            min="0"
            value={form.quick_profit_usd}
            onChange={(e) => update("quick_profit_usd", e.target.value)}
          />
        </label>
        <label>
          Auto-close after (minutes, 0 = off)
          <input
            disabled={running}
            type="number"
            min="0"
            value={form.max_trade_minutes}
            onChange={(e) => update("max_trade_minutes", e.target.value)}
          />
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
        <label className="settings-span">
          <input
            disabled={running}
            type="checkbox"
            checked={Boolean(form.basket_mode)}
            onChange={(e) => update("basket_mode", e.target.checked)}
          />{" "}
          Basket mode — stack several entries and close them together
        </label>
        <label>
          Basket: max entries
          <input
            disabled={running || !form.basket_mode}
            type="number"
            min="1"
            value={form.basket_max_entries}
            onChange={(e) => update("basket_max_entries", e.target.value)}
          />
        </label>
        <label>
          Basket: gap before adding (points)
          <input
            disabled={running || !form.basket_mode}
            type="number"
            min="1"
            value={form.basket_add_gap_points}
            onChange={(e) => update("basket_add_gap_points", e.target.value)}
          />
        </label>
        <label>
          Basket: close group at $ profit
          <input
            disabled={running || !form.basket_mode}
            type="number"
            step="0.5"
            min="0.5"
            value={form.basket_target_usd}
            onChange={(e) => update("basket_target_usd", e.target.value)}
          />
        </label>
        <label>
          Basket: group stop at $ loss
          <input
            disabled={running || !form.basket_mode}
            type="number"
            step="1"
            min="1"
            value={form.basket_max_loss_usd}
            onChange={(e) => update("basket_max_loss_usd", e.target.value)}
          />
        </label>
        <button className="btn btn-primary" type="submit" disabled={running || saving}>
          {saving ? "Saving..." : "Save Settings"}
        </button>
      </form>
    </div>
  );
}
