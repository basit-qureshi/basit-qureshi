const BASE_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

async function request(path, options = {}) {
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || `Request failed: ${res.status}`);
  }
  return data;
}

export const api = {
  getStatus: () => request("/api/status"),
  start: (confirmReal = false) =>
    request("/api/start", { method: "POST", body: JSON.stringify({ confirm_real: confirmReal }) }),
  stop: () => request("/api/stop", { method: "POST" }),
  clearHalt: () => request("/api/clear-halt", { method: "POST" }),
  // Every filter is optional; blanks are dropped so the query string only
  // carries what was actually chosen.
  getTrades: (params = {}) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null && value !== "" && value !== "ALL" && value !== "all") {
        query.set(key, value);
      }
    }
    const qs = query.toString();
    return request(`/api/trades${qs ? `?${qs}` : ""}`);
  },
  getTradingDays: () => request("/api/trading-days"),
  getOpenTrades: () => request("/api/open-trades"),
  getCandles: (count = 200) => request(`/api/candles?count=${count}`),
  getStats: () => request("/api/stats"),
  getSettings: () => request("/api/settings"),
  updateSettings: (settings) => request("/api/settings", { method: "POST", body: JSON.stringify(settings) }),
  setMode: (mode, confirm = false) =>
    request("/api/mode", { method: "POST", body: JSON.stringify({ mode, confirm }) }),
  runBacktest: (params) => request("/api/backtest", { method: "POST", body: JSON.stringify(params) }),
  testOrder: (side, volume = 0.01, confirmReal = false) =>
    request("/api/test-order", { method: "POST", body: JSON.stringify({ side, volume, confirm_real: confirmReal }) }),
};

export function connectWebSocket(onMessage) {
  const wsUrl = BASE_URL.replace(/^http/, "ws") + "/ws";
  let socket;
  let closedByUser = false;

  function connect() {
    socket = new WebSocket(wsUrl);
    socket.onmessage = (event) => {
      try {
        onMessage(JSON.parse(event.data));
      } catch {
        // ignore malformed frames
      }
    };
    socket.onclose = () => {
      if (!closedByUser) setTimeout(connect, 2000);
    };
  }
  connect();

  return () => {
    closedByUser = true;
    socket && socket.close();
  };
}
