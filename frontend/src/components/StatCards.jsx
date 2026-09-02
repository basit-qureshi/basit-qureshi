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
  // Split daily figures. today_profit is kept as the net value for anything
  // still reading the old field.
  const todayGross = stats?.today_gross_profit_usd ?? 0;
  const todayLoss = stats?.today_gross_loss_usd ?? 0;
  const todayNet = stats?.today_net_profit_usd ?? stats?.today_profit ?? 0;
  const dailyTarget = stats?.daily_target ?? 0;
  const unsettled = stats?.today_unsettled_trades ?? 0;
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
      <Card label="Today's Profit" value={fmt(todayGross)} tone="tone-green" />
      <Card label="Today's Loss" value={fmt(-todayLoss)} tone="tone-red" />
      <Card label="Today's Net P&L" value={fmt(todayNet)} tone={todayNet >= 0 ? "tone-green" : "tone-red"} />
      {dailyTarget > 0 && (
        <Card
          label="Daily Target"
          value={`${fmt(todayNet)} of ${fmt(dailyTarget)}`}
          tone={stats?.daily_target_hit ? "tone-green" : ""}
        />
      )}
      {unsettled > 0 && <Card label="Awaiting Result" value={`${unsettled} trade${unsettled === 1 ? "" : "s"}`} />}
      <Card label="Win Rate" value={`${winRate}%`} />
      <Card label="Open Trades" value={openCount} />
      <Card label="Total Trades" value={stats?.total_trades ?? 0} />
      <Card label="Avg Win" value={fmt(stats?.avg_win ?? 0)} tone="tone-green" />
      <Card label="Avg Loss" value={fmt(-(stats?.avg_loss ?? 0))} tone="tone-red" />
      <Card label="Profit Factor" value={stats?.profit_factor ?? "—"} />
    </div>
  );
}
