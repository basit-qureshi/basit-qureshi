function Card({ label, value, tone }) {
  return (
    <div className="card">
      <div className="card-label">{label}</div>
      <div className={`card-value ${tone || ""}`}>{value}</div>
    </div>
  );
}

export default function StatCards({ account, stats, liveOpenPositions }) {
  const balance = account?.balance ?? 0;
  const equity = account?.equity ?? 0;
  const currency = account?.currency ?? "USD";
  const todayProfit = stats?.today_profit ?? 0;
  const winRate = stats?.win_rate ?? 0;
  // Prefer the live WebSocket feed (only present while the bot is actively running),
  // otherwise fall back to the DB-backed stats count so a stopped/just-loaded bot
  // still shows the correct number of currently open trades.
  const openCount = liveOpenPositions && liveOpenPositions.length > 0 ? liveOpenPositions.length : (stats?.open_trades ?? 0);

  const fmt = (n) => `${n >= 0 ? "" : "-"}$${Math.abs(n).toFixed(2)}`;

  return (
    <div className="stat-grid">
      <Card label="Balance" value={`${fmt(balance)} ${currency}`} />
      <Card label="Equity" value={`${fmt(equity)} ${currency}`} tone={equity >= balance ? "tone-green" : "tone-red"} />
      <Card label="Today's P&L" value={fmt(todayProfit)} tone={todayProfit >= 0 ? "tone-green" : "tone-red"} />
      <Card label="Win Rate" value={`${winRate}%`} />
      <Card label="Open Trades" value={openCount} />
      <Card label="Total Trades" value={stats?.total_trades ?? 0} />
    </div>
  );
}
