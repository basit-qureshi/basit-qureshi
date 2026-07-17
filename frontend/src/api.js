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
  getTrades: (limit = 100) => request(`/api/trades?limit=${limit}`),
  getCandles: (count = 200) => request(`/api/candles?count=${count}`),
  getStats: () => request("/api/stats"),
  getSettings: () => request("/api/settings"),
  updateSettings: (settings) => request("/api/settings", { method: "POST", body: JSON.stringify(settings) }),
  setMode: (mode, confirm = false) =>
    request("/api/mode", { method: "POST", body: JSON.stringify({ mode, confirm }) }),
  runBacktest: (params) => request("/api/backtest", { method: "POST", body: JSON.stringify(params) }),
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
