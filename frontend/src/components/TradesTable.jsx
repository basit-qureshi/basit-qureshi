import { formatTime } from "../time";

const PAGE_SIZES = [25, 50, 100, 250];

/**
 * Trade history with filters and paging.
 *
 * The filters and the page are held by the parent and sent to the server, so
 * the rows and the summary above them come from one query. Filtering in the
 * browser over a truncated list would show a summary that describes something
 * other than the account.
 */
export default function TradesTable({ page, filters, onFilterChange, onPageChange, days, loading }) {
  const items = page?.items || [];
  const sel = page?.selection;
  const pages = page?.pages ?? 1;
  const current = page?.page ?? 1;

  const set = (key) => (event) => onFilterChange({ ...filters, [key]: event.target.value });

  const quickRange = (fromDay, toDay) => () =>
    onFilterChange({ ...filters, date_from: fromDay || "", date_to: toDay || "" });

  const today = days?.last || "";
  const earliest = days?.first || "";

  return (
    <div className="panel trades-panel">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">ACTIVITY</span>
          <h3>Trade history</h3>
        </div>
        <span className="timezone-label">Pakistan time · UTC+5</span>
      </div>

      <div className="filter-bar">
        <label>
          <span>From</span>
          <input type="date" value={filters.date_from} min={earliest} max={today} onChange={set("date_from")} />
        </label>
        <label>
          <span>To</span>
          <input type="date" value={filters.date_to} min={earliest} max={today} onChange={set("date_to")} />
        </label>
        <label>
          <span>Status</span>
          <select value={filters.status} onChange={set("status")}>
            <option value="ALL">All</option>
            <option value="OPEN">Open</option>
            <option value="CLOSED">Closed</option>
          </select>
        </label>
        <label>
          <span>Side</span>
          <select value={filters.side} onChange={set("side")}>
            <option value="ALL">All</option>
            <option value="BUY">Buy</option>
            <option value="SELL">Sell</option>
          </select>
        </label>
        <label>
          <span>Result</span>
          <select value={filters.result} onChange={set("result")}>
            <option value="all">All</option>
            <option value="win">Winners</option>
            <option value="loss">Losers</option>
            <option value="breakeven">Break-even</option>
            <option value="unsettled">Not settled</option>
          </select>
        </label>
        <label className="filter-search">
          <span>Search</span>
          <input
            type="search"
            placeholder="ticket or reason"
            value={filters.search}
            onChange={set("search")}
          />
        </label>
        <label>
          <span>Per page</span>
          <select value={filters.page_size} onChange={set("page_size")}>
            {PAGE_SIZES.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="filter-quick">
        {/* Only days the account actually has trades on, so a range can never
            select nothing by accident. */}
        <button type="button" className="chip" onClick={quickRange(today, today)} disabled={!today}>
          Today
        </button>
        <button type="button" className="chip" onClick={quickRange(earliest, today)} disabled={!earliest}>
          All days
        </button>
        <button
          type="button"
          className="chip"
          onClick={() =>
            onFilterChange({ date_from: "", date_to: "", status: "ALL", side: "ALL", result: "all", search: "", page_size: filters.page_size })
          }
        >
          Clear filters
        </button>
        {days?.days?.length > 0 && (
          <span className="muted">
            {days.days.length} trading day{days.days.length === 1 ? "" : "s"} on record
          </span>
        )}
      </div>

      {sel && (
        <div className="selection-summary">
          <div>
            <span>Selected</span>
            <b>{page.total}</b>
          </div>
          <div>
            <span>Settled</span>
            <b>{sel.settled}</b>
          </div>
          <div>
            <span>Wins</span>
            <b className="tone-green">{sel.wins}</b>
          </div>
          <div>
            <span>Losses</span>
            <b className="tone-red">{sel.losses}</b>
          </div>
          <div>
            <span>Win rate</span>
            <b>{sel.win_rate}%</b>
          </div>
          <div>
            <span>Gross profit</span>
            <b className="tone-green">${sel.gross_profit.toFixed(2)}</b>
          </div>
          <div>
            <span>Gross loss</span>
            <b className="tone-red">${sel.gross_loss.toFixed(2)}</b>
          </div>
          <div>
            <span>Net</span>
            <b className={sel.net_profit >= 0 ? "tone-green" : "tone-red"}>${sel.net_profit.toFixed(2)}</b>
          </div>
          {sel.unsettled > 0 && (
            <div>
              {/* Counted in the total but not in wins, losses or the rate. */}
              <span>Not settled</span>
              <b>{sel.unsettled}</b>
            </div>
          )}
        </div>
      )}

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Day</th>
              <th>Side</th>
              <th>Volume</th>
              <th>Open</th>
              <th>Close</th>
              <th>Status</th>
              <th>Profit</th>
              <th>Why it ended</th>
              <th>Opened (PKT)</th>
              <th>Closed (PKT)</th>
            </tr>
          </thead>
          <tbody>
            {items.length === 0 && (
              <tr>
                <td colSpan={10} className="muted">
                  {loading ? "Loading…" : page?.total === 0 && hasFilters(filters)
                    ? "No trades match these filters."
                    : "No trades yet."}
                </td>
              </tr>
            )}
            {items.map((t) => (
              <tr key={t.id}>
                <td>{t.trading_day || "—"}</td>
                <td className={t.side === "BUY" ? "tone-green" : "tone-red"}>{t.side}</td>
                <td>{t.volume}</td>
                <td>{t.open_price?.toFixed(2)}</td>
                <td>{t.close_price != null ? t.close_price.toFixed(2) : "—"}</td>
                <td>
                  <span className={`badge ${t.status === "OPEN" ? "badge-demo" : "badge-closed"}`}>{t.status}</span>
                </td>
                <td className={t.profit == null ? "" : t.profit >= 0 ? "tone-green" : "tone-red"}>
                  {t.profit == null ? "—" : `$${t.profit.toFixed(2)}`}
                </td>
                <td className="reason-cell" title={t.close_reason || ""}>
                  {t.close_reason || (t.status === "OPEN" ? "still open" : "—")}
                </td>
                <td>{formatTime(t.open_time)}</td>
                <td>{formatTime(t.close_time)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="pager">
        <button type="button" className="btn btn-ghost" disabled={current <= 1} onClick={() => onPageChange(1)}>
          « First
        </button>
        <button type="button" className="btn btn-ghost" disabled={current <= 1} onClick={() => onPageChange(current - 1)}>
          ‹ Prev
        </button>
        <span className="pager-state">
          Page {current} of {pages}
          {page?.total != null && ` · ${page.total} trade${page.total === 1 ? "" : "s"}`}
        </span>
        <button
          type="button"
          className="btn btn-ghost"
          disabled={current >= pages}
          onClick={() => onPageChange(current + 1)}
        >
          Next ›
        </button>
        <button
          type="button"
          className="btn btn-ghost"
          disabled={current >= pages}
          onClick={() => onPageChange(pages)}
        >
          Last »
        </button>
      </div>
    </div>
  );
}

function hasFilters(f) {
  return Boolean(f.date_from || f.date_to || f.search || f.status !== "ALL" || f.side !== "ALL" || f.result !== "all");
}
