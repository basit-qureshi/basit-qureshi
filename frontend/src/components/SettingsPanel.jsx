import { useEffect, useState } from "react";

const SYMBOLS = ["XAUUSD", "XAUUSDm"];

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

  const levels = Number(form.grid_buy_stop_levels || 0) + Number(form.grid_sell_stop_levels || 0);
  const maxLots = (levels * Number(form.grid_lot_size || 0)).toFixed(2);
  const perDollar = (maxLots * 100).toFixed(0); // gold: 1.00 lot is $100 per $1 of price

  async function handleSubmit(e) {
    e.preventDefault();
    const ok = await onSave({
      symbol: form.symbol,
      poll_interval_seconds: Number(form.poll_interval_seconds),
      grid_lot_size: Number(form.grid_lot_size),
      grid_buy_stop_levels: Number(form.grid_buy_stop_levels),
      grid_sell_stop_levels: Number(form.grid_sell_stop_levels),
      grid_distance: Number(form.grid_distance),
      grid_basket_take_profit_usd: Number(form.grid_basket_take_profit_usd),
      grid_daily_profit_target_usd: Number(form.grid_daily_profit_target_usd),
      grid_basket_stop_loss_usd: Number(form.grid_basket_stop_loss_usd),
      grid_max_open_positions: Number(form.grid_max_open_positions),
      grid_max_daily_loss_usd: Number(form.grid_max_daily_loss_usd),
      grid_max_equity_drawdown_percent: Number(form.grid_max_equity_drawdown_percent),
      grid_magic_number: Number(form.grid_magic_number),
      grid_trading_start_hour: Number(form.grid_trading_start_hour),
      grid_trading_end_hour: Number(form.grid_trading_end_hour),
    });
    if (ok) setDirty(false); // keep unsaved edits on screen if the save was rejected
  }

  return (
    <div className="panel">
      <h3>Grid Settings</h3>
      <p className="muted">
        The bot runs one strategy: a <b>pending-order grid</b> on XAUUSD M1. It places{" "}
        <i>Buy stop levels</i> BUY STOPs above the market and <i>Sell stop levels</i> SELL STOPs below it, all at
        the same fixed lot. As price moves, those stops become positions. The moment the <b>combined</b> profit of
        every open position reaches <i>Basket take profit</i>, all of them close together, every remaining pending
        order is cancelled, and a fresh grid is built around the new price. No single trade has to reach the target
        on its own.
      </p>
      <p className="muted">
        <b>Daily profit target</b> stops the bot for the rest of the broker day once that much{" "}
        <i>net realized</i> profit has been booked — settled trades after their losses, commission and swap, never
        floating profit. It is judged on this bot's own trades only, so a manual order or a test trade cannot move
        it, and the lock is worked out from those trades rather than remembered in memory: restarting the backend,
        refreshing the page or pressing Start again will not get past it. Set it to 0 to switch it off.
      </p>
      <p className="muted">
        A fresh grid is never placed on the same M1 candle the bot became ready on — whether that was Start Bot, a
        basket closing, or the grid being deleted by hand in MT5. It waits for the broker to confirm a later candle.
      </p>
      <p className="muted">
        There are no indicators and no per-trade stop loss or take profit. A grid position is only ever closed by
        the basket rule or by the risk limits below.
      </p>
      <p className="muted warn-text">
        ⚠ Know this number before you start. With your current settings a fully triggered grid is{" "}
        <b>{maxLots} lots</b>, which on gold is about <b>${perDollar} for every $1 the price moves</b>. There is no
        stop on an individual trade, so a basket that never reaches its target keeps growing while price runs. The
        only things that end it are <i>Basket stop loss</i>, <i>Max daily loss</i> and <i>Max equity drawdown</i> —
        they are the whole risk model, not optional extras. Set them to money you are genuinely willing to lose, and
        test on demo for a meaningful number of baskets first.
      </p>
      {running && <p className="muted">Stop the bot to change settings.</p>}
      <form className="settings-form" onSubmit={handleSubmit}>
        <label>
          Symbol (your broker's exact name)
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
          Lot size (every order)
          <input
            disabled={running}
            type="number"
            step="0.01"
            min="0.01"
            value={form.grid_lot_size}
            onChange={(e) => update("grid_lot_size", e.target.value)}
          />
        </label>
        <label>
          Buy stop levels
          <input
            disabled={running}
            type="number"
            min="1"
            value={form.grid_buy_stop_levels}
            onChange={(e) => update("grid_buy_stop_levels", e.target.value)}
          />
        </label>
        <label>
          Sell stop levels
          <input
            disabled={running}
            type="number"
            min="1"
            value={form.grid_sell_stop_levels}
            onChange={(e) => update("grid_sell_stop_levels", e.target.value)}
          />
        </label>
        <label>
          Grid distance (price, 0.30 = 30 points)
          <input
            disabled={running}
            type="number"
            step="0.01"
            min="0.01"
            value={form.grid_distance}
            onChange={(e) => update("grid_distance", e.target.value)}
          />
        </label>
        <label>
          Basket take profit ($, combined)
          <input
            disabled={running}
            type="number"
            step="0.5"
            min="0.5"
            value={form.grid_basket_take_profit_usd}
            onChange={(e) => update("grid_basket_take_profit_usd", e.target.value)}
          />
        </label>

        <label>
          Daily profit target ($, net realized, 0 = off)
          <input
            disabled={running}
            type="number"
            step="0.5"
            min="0"
            value={form.grid_daily_profit_target_usd}
            onChange={(e) => update("grid_daily_profit_target_usd", e.target.value)}
          />
        </label>

        <label className="settings-span">Risk limits — the only thing that ends a losing basket</label>
        <label>
          Basket stop loss ($, 0 = off)
          <input
            disabled={running}
            type="number"
            step="1"
            min="0"
            value={form.grid_basket_stop_loss_usd}
            onChange={(e) => update("grid_basket_stop_loss_usd", e.target.value)}
          />
        </label>
        <label>
          Max open positions
          <input
            disabled={running}
            type="number"
            min="1"
            value={form.grid_max_open_positions}
            onChange={(e) => update("grid_max_open_positions", e.target.value)}
          />
        </label>
        <label>
          Max daily loss ($)
          <input
            disabled={running}
            type="number"
            step="1"
            min="0"
            value={form.grid_max_daily_loss_usd}
            onChange={(e) => update("grid_max_daily_loss_usd", e.target.value)}
          />
        </label>
        <label>
          Max equity drawdown (%)
          <input
            disabled={running}
            type="number"
            step="1"
            min="0"
            value={form.grid_max_equity_drawdown_percent}
            onChange={(e) => update("grid_max_equity_drawdown_percent", e.target.value)}
          />
        </label>

        <label className="settings-span">Session &amp; identity</label>
        <label>
          Trading start hour (UTC)
          <input
            disabled={running}
            type="number"
            min="0"
            max="24"
            value={form.grid_trading_start_hour}
            onChange={(e) => update("grid_trading_start_hour", e.target.value)}
          />
        </label>
        <label>
          Trading end hour (UTC, 24 = always)
          <input
            disabled={running}
            type="number"
            min="0"
            max="24"
            value={form.grid_trading_end_hour}
            onChange={(e) => update("grid_trading_end_hour", e.target.value)}
          />
        </label>
        <label>
          Magic number
          <input
            disabled={running}
            type="number"
            value={form.grid_magic_number}
            onChange={(e) => update("grid_magic_number", e.target.value)}
          />
        </label>
        <label>
          Poll interval (seconds)
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
