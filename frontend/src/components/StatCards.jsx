function Card({ label, value, tone = "", hint }) {
  return <div className="card"><div className="card-label">{label}</div><div className={"card-value " + tone}>{value}</div>{hint && <div className="card-hint">{hint}</div>}</div>;
}
const money = (n = 0) => (n < 0 ? "−" : "") + "$" + Math.abs(n).toFixed(2);

export default function StatCards({ account, stats, liveOpenPositions }) {
  const balance = account?.balance ?? 0, equity = account?.equity ?? 0;
  const net = stats?.today_net_profit_usd ?? stats?.today_profit ?? 0;
  const open = liveOpenPositions == null ? stats?.open_trades ?? 0 : liveOpenPositions.length;
  return <>
    <div className="stat-grid primary-stats">
      <Card label="Account balance" value={account ? money(balance) : "…"} hint={account?.currency || "USD"} />
      <Card label="Live equity" value={account ? money(equity) : "…"} tone={equity >= balance ? "tone-green" : "tone-red"} hint="Includes floating P&L" />
      <Card label="Today’s net P&L" value={stats ? money(net) : "…"} tone={net >= 0 ? "tone-green" : "tone-red"} hint={stats?.daily_target > 0 ? "Daily target " + money(stats.daily_target) : "Realized profit after losses"} />
      <Card label="Open positions" value={open} hint={(stats?.total_trades ?? 0) + " total trades"} />
    </div>
    <div className="stat-grid secondary-stats">
      <Card label="Today’s profit" value={money(stats?.today_gross_profit_usd)} tone="tone-green" />
      <Card label="Today’s loss" value={money(-(stats?.today_gross_loss_usd ?? 0))} tone="tone-red" />
      <Card label="Win rate" value={(stats?.win_rate ?? 0) + "%"} />
      <Card label="Average win" value={money(stats?.avg_win)} tone="tone-green" />
      <Card label="Average loss" value={money(-(stats?.avg_loss ?? 0))} tone="tone-red" />
      <Card label="Profit factor" value={stats?.profit_factor ?? "—"} />
    </div>
    {stats?.today_unsettled_trades > 0 && <p className="muted">{stats.today_unsettled_trades} trade results are awaiting broker settlement.</p>}
  </>;
}
