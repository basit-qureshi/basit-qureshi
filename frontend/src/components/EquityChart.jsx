import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis, CartesianGrid } from "recharts";

export default function EquityChart({ data, title = "Equity Curve" }) {
  const points = (data || []).map((p, i) => ({ index: i, equity: p.equity, time: p.time }));

  return (
    <div className="panel">
      <h3>{title}</h3>
      {points.length === 0 ? (
        <p className="muted">No closed trades yet — the equity curve will appear once trades start closing.</p>
      ) : (
        <ResponsiveContainer width="100%" height={260}>
          <LineChart data={points}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
            <XAxis dataKey="index" tick={false} />
            <YAxis domain={["auto", "auto"]} width={70} tick={{ fill: "var(--muted)", fontSize: 12 }} />
            <Tooltip
              contentStyle={{ background: "var(--surface)", border: "1px solid var(--border)" }}
              formatter={(value) => [`$${value}`, "Equity"]}
              labelFormatter={() => ""}
            />
            <Line type="monotone" dataKey="equity" stroke="var(--accent)" dot={false} strokeWidth={2} />
          </LineChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
