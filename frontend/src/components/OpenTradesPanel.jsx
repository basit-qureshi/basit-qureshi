import { formatTime } from "../time";

/**
 * What the bot is holding right now, as cards rather than a scrolling table.
 *
 * The point of this panel is that nothing important needs scrolling to. The
 * worst position sorts first, and the figure shown large is the NET one after
 * swap and commission — the same number the basket rule is judged on, so this
 * panel and the basket total can never tell different stories.
 */
export default function OpenTradesPanel({ data }) {
  const positions = data?.positions || [];
  const totals = data?.totals;
  const target = totals?.target ?? 0;
  const judged = totals?.after_exit_cost ?? 0;

  return (
    <section className="panel open-trades-panel">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">LIVE EXPOSURE</span>
          <h3>Open trades</h3>
        </div>
        <span className="timezone-label">Pakistan time · UTC+5</span>
      </div>

      <div className="open-summary">
        <div className="open-summary-item">
          <span>Positions</span>
          <b>{totals?.count ?? 0}</b>
        </div>
        <div className="open-summary-item">
          <span>Total volume</span>
          <b>{(totals?.volume ?? 0).toFixed(2)} lots</b>
        </div>
        <div className="open-summary-item">
          <span>Resting orders</span>
          <b>
            {data?.buy_stops ?? 0} buy / {data?.sell_stops ?? 0} sell
          </b>
        </div>
        <div className="open-summary-item">
          <span>Gross</span>
          <b className={(totals?.gross_profit ?? 0) >= 0 ? "tone-green" : "tone-red"}>
            ${(totals?.gross_profit ?? 0).toFixed(2)}
          </b>
        </div>
        <div className="open-summary-item">
          <span>Net after costs</span>
          <b className={(totals?.net_profit ?? 0) >= 0 ? "tone-green" : "tone-red"}>
            ${(totals?.net_profit ?? 0).toFixed(2)}
          </b>
        </div>
        <div className="open-summary-item">
          {/* This is the number the basket target is measured against, so it is
              labelled as such rather than left to be inferred. */}
          <span>Judged on (after exit cost)</span>
          <b className={judged >= 0 ? "tone-green" : "tone-red"}>
            ${judged.toFixed(2)}
            {target > 0 && <em className="muted"> / ${target.toFixed(2)}</em>}
          </b>
        </div>
      </div>

      {totals?.costs_known === false && (
        <p className="muted">
          ⚠ The broker did not report swap or commission for at least one position, so the net
          figure above is the best available estimate rather than an exact one.
        </p>
      )}

      {data?.connected === false && (
        <p className="error-text">Not connected to the broker — this panel cannot show live exposure.</p>
      )}

      {positions.length === 0 ? (
        <p className="muted open-empty">
          {data?.connected === false ? "" : "No open positions. Nothing is at risk right now."}
        </p>
      ) : (
        <div className="open-trades-grid">
          {positions.map((p) => (
            <article key={p.ticket} className={`open-card ${p.net_profit >= 0 ? "is-up" : "is-down"}`}>
              <header>
                <span className={p.side === "BUY" ? "badge badge-buy" : "badge badge-sell"}>{p.side}</span>
                <span className="open-card-vol">{p.volume} lots</span>
              </header>
              <div className={`open-card-pnl ${p.net_profit >= 0 ? "tone-green" : "tone-red"}`}>
                {p.net_profit < 0 ? "−" : "+"}${Math.abs(p.net_profit).toFixed(2)}
              </div>
              <dl className="open-card-detail">
                <div>
                  <dt>Entry</dt>
                  <dd>{p.open_price?.toFixed(2)}</dd>
                </div>
                <div>
                  <dt>Now</dt>
                  <dd>{p.current_price ? p.current_price.toFixed(2) : "—"}</dd>
                </div>
                <div>
                  <dt>Gross</dt>
                  <dd>${p.gross_profit.toFixed(2)}</dd>
                </div>
                <div>
                  <dt>Swap + comm.</dt>
                  <dd>${(p.swap + p.commission).toFixed(2)}</dd>
                </div>
              </dl>
              <footer className="muted">
                {p.open_time ? `Opened ${formatTime(p.open_time)}` : `Ticket ${p.ticket}`}
              </footer>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
