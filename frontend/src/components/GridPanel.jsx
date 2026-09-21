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
      {status?.day_risk && (
        <div className="risk-readout">
          <div className="risk-heading">
            Daily risk reading
            <span className="muted"> · day cut on {status.accounting_timezone} time</span>
          </div>
          {/* These two are DIFFERENT measurements and are not expected to
              agree while positions are open. The realised card is settled
              trades only; the marked reading adds today's change in open
              exposure, which is what the daily limit is judged on. */}
          <dl className="risk-rows">
            <div>
              <dt>Settled today (realised)</dt>
              <dd>${status.day_risk.settled_realized_usd?.toFixed(2)}</dd>
            </div>
            <div>
              <dt>Open mark change today</dt>
              <dd>${status.day_risk.open_mark_change_usd?.toFixed(2)}</dd>
            </div>
            {status.day_risk.pending_settlement_marked_usd !== 0 && (
              <div>
                <dt>Closed, not settled yet</dt>
                <dd>${status.day_risk.pending_settlement_marked_usd?.toFixed(2)}</dd>
              </div>
            )}
            <div className="risk-total">
              <dt>Marked result (limit is judged on this)</dt>
              <dd className={status.day_risk.marked_result_usd >= 0 ? "tone-green" : "tone-red"}>
                ${status.day_risk.marked_result_usd?.toFixed(2)}
              </dd>
            </div>
            <div>
              <dt>Exit reserve (estimate, shown apart)</dt>
              <dd>
                {status.day_risk.exit_reserve_usd == null
                  ? "unknown"
                  : `$${status.day_risk.exit_reserve_usd.toFixed(2)}`}
              </dd>
            </div>
            {status.daily_limit_remaining_usd != null && (
              <div>
                <dt>Daily limit remaining</dt>
                <dd className={status.daily_limit_remaining_usd > 0 ? "" : "tone-red"}>
                  ${status.daily_limit_remaining_usd.toFixed(2)}
                </dd>
              </div>
            )}
          </dl>
          {status.day_risk.complete === false && (
            <p className="error-text">
              ⚠ Today's accounting is incomplete, so no new grid may be placed:{" "}
              {status.day_risk.incomplete_reasons?.join("; ")}
            </p>
          )}
        </div>
      )}
      {status?.broker_owner && (
        <div className="risk-readout">
          <div className="risk-heading">Execution health</div>
          <dl className="risk-rows">
            <div>
              <dt>Protective check every</dt>
              <dd>{status.protective_poll_seconds}s{status.protective_backoff_seconds > 0
                ? ` (backing off to ${status.protective_backoff_seconds}s)` : ""}</dd>
            </div>
            <div>
              <dt>Reporting every</dt>
              <dd>{status.reporting_poll_seconds}s</dd>
            </div>
            <div>
              {/* Local age from a monotonic clock. NOT a network latency
                  figure: the terminal's clock and this machine's are not
                  synchronised, so their difference is not measurable delay. */}
              <dt>Last quote age (local clock)</dt>
              <dd>{status.quote_missing
                ? "no quote"
                : status.quote_local_age_ms == null ? "—" : `${Math.round(status.quote_local_age_ms)} ms`}</dd>
            </div>
            {status.reporting_cycles_skipped > 0 && (
              <div>
                <dt>Reporting cycles skipped</dt>
                <dd>{status.reporting_cycles_skipped}</dd>
              </div>
            )}
          </dl>
          {status.broker_owner.blocked && (
            <p className="error-text">
              ⛔ A broker call ({status.broker_owner.in_flight}) has been running for{" "}
              {(status.broker_owner.in_flight_ms / 1000).toFixed(1)}s and has not returned.
              No new exposure until it does, and no second request is sent while the first
              may still reach the broker.
            </p>
          )}
          {status.broker_owner.last_error && !status.broker_owner.blocked && (
            <p className="muted">Last broker error: {status.broker_owner.last_error}</p>
          )}
        </div>
      )}
      {grid?.last_event && <p className="cycle-event">{grid.last_event}</p>}
    </section>
  );
}
