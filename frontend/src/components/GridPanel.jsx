export default function GridPanel({ grid, status , onClearHalt }) {
  const settings = status?.settings || {};
  const target = grid?.target ?? settings.grid_basket_take_profit_usd ?? 10;
  const profit = grid?.basket_profit ?? 0;
  const progress = target > 0 ? Math.max(0, Math.min(100, profit / target * 100)) : 0;
  const phase = !status?.running ? "Stopped" : grid?.halted ? "Halted" : status?.closing_profit_basket ? "Closing basket" : "Running";
  return (
    <section className="panel grid-panel">
      <div className="panel-heading"><div><span className="eyebrow">CURRENT CYCLE</span><h3>Basket overview</h3></div><span className="badge badge-demo">{phase}</span></div>
      <div className={"basket-value " + (profit >= 0 ? "tone-green" : "tone-red")}>{profit < 0 ? "−" : "+"}$ {Math.abs(profit).toFixed(2)}</div>
      <div className="progress-caption"><span>Combined profit</span><b>$ {Number(target).toFixed(2)} target</b></div>
      <progress className="basket-progress" max="100" value={progress} aria-label="Basket target progress" />
      <div className="cycle-counts">
        <div><span className="tone-green">BUY STOPS</span><b>{grid?.buy_stops ?? 0}</b></div>
        <div><span className="tone-red">SELL STOPS</span><b>{grid?.sell_stops ?? 0}</b></div>
        <div><span>OPEN TRADES</span><b>{grid?.open_positions ?? 0}</b></div>
      </div>
      <dl className="grid-details">
        <div><dt>Lot per order</dt><dd>{Number(settings.grid_lot_size ?? .01).toFixed(2)}</dd></div>
        <div><dt>Grid distance</dt><dd>{Number(settings.grid_distance ?? .30).toFixed(2)}</dd></div>
        <div><dt>Configured levels</dt><dd>{settings.grid_buy_stop_levels ?? 10} buy / {settings.grid_sell_stop_levels ?? 10} sell</dd></div>
        <div><dt>Closed at target</dt><dd>{grid?.baskets_won ?? status?.baskets_won ?? 0}</dd></div>
        {grid?.reference_price && <div><dt>Reference price</dt><dd>{grid.reference_price.toFixed(2)}</dd></div>}
      </dl>
      <div className="cycle-note"><span className="dot dot-green" />Same candle restart after profit</div>
      {grid?.daily_target_hit && <p className="tone-green">Daily target reached. Trading paused for today.</p>}
      {grid?.waiting_reason && <p className="muted">{grid.waiting_reason}</p>}
      {/* A refusal the owner cannot see looks like a broken bot. The reason the
          grid was not placed is shown with the same weight as a halt. */}
      {status?.entry_block_reason && (
        <p className="error-text">⛔ Entries refused — {status.entry_block_reason}</p>
      )}
      {status?.trading_window && status.trading_window !== "always on" && (
        <p className="muted">
          Trading window: {status.trading_window}
          {status.in_session === false ? " · outside it now, no new grids" : " · inside it now"}
        </p>
      )}
      {grid?.halted && (
        <>
          <p className="error-text">⛔ Halted — {grid.halted}</p>
          {onClearHalt && (
            <button className="btn btn-ghost" onClick={onClearHalt}>
              Clear halt (only once this bot is flat)
            </button>
          )}
        </>
      )}
      {grid?.last_event && <p className="cycle-event">{grid.last_event}</p>}
    </section>
  );
}
