export default function TradesTable({ trades }) {
  return (
    <div className="panel">
      <h3>Trade History</h3>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Side</th>
              <th>Volume</th>
              <th>Open Price</th>
              <th>SL</th>
              <th>TP</th>
              <th>Status</th>
              <th>Profit</th>
              <th>Mode</th>
              <th>Opened</th>
            </tr>
          </thead>
          <tbody>
            {(!trades || trades.length === 0) && (
              <tr>
                <td colSpan={10} className="muted">
                  No trades yet.
                </td>
              </tr>
            )}
            {trades?.map((t) => (
              <tr key={t.id}>
                <td>{t.symbol}</td>
                <td className={t.side === "BUY" ? "tone-green" : "tone-red"}>{t.side}</td>
                <td>{t.volume}</td>
                <td>{t.open_price?.toFixed(5)}</td>
                <td>{t.sl?.toFixed(5)}</td>
                <td>{t.tp?.toFixed(5)}</td>
                <td>
                  <span className={`badge ${t.status === "OPEN" ? "badge-demo" : "badge-closed"}`}>{t.status}</span>
                </td>
                <td className={t.profit == null ? "" : t.profit >= 0 ? "tone-green" : "tone-red"}>
                  {t.profit == null ? "—" : `$${t.profit.toFixed(2)}`}
                </td>
                <td>{t.mode}</td>
                <td>{t.open_time ? new Date(t.open_time).toLocaleString() : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
