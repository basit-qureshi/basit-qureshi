import { useEffect, useState, useCallback, useRef } from "react";
import { api, connectWebSocket } from "./api";
import StatusBar from "./components/StatusBar";
import StatCards from "./components/StatCards";
import LiveChart from "./components/LiveChart";
import EquityChart from "./components/EquityChart";
import TradesTable from "./components/TradesTable";
import SettingsPanel from "./components/SettingsPanel";
import BacktestPanel from "./components/BacktestPanel";
import ManualTestPanel from "./components/ManualTestPanel";
import Toasts from "./components/Toasts";
import "./App.css";

export default function App() {
  const [status, setStatus] = useState(null);
  const [stats, setStats] = useState(null);
  const [trades, setTrades] = useState([]);
  const [liveAccount, setLiveAccount] = useState(null);
  const [liveOpenPositions, setLiveOpenPositions] = useState([]);
  const [lastSignal, setLastSignal] = useState(null);
  const [basket, setBasket] = useState(null);
  const [setup, setSetup] = useState(null);
  const [busy, setBusy] = useState(false);
  const [globalError, setGlobalError] = useState(null);
  const [tab, setTab] = useState("dashboard");
  const [toasts, setToasts] = useState([]);
  const prevTradesRef = useRef(null);

  const pushToast = useCallback((kind, title, body) => {
    const id = `${Date.now()}-${Math.random()}`;
    setToasts((t) => [...t, { id, kind, title, body }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 8000);
  }, []);

  const dismissToast = useCallback((id) => {
    setToasts((t) => t.filter((x) => x.id !== id));
  }, []);

  // Compare each trades poll against the previous one to announce what changed.
  // Skipped on the very first load so a page refresh doesn't replay history.
  const announceTradeChanges = useCallback(
    (newTrades) => {
      const prev = prevTradesRef.current;
      prevTradesRef.current = newTrades;
      if (prev === null) return;
      const prevById = new Map(prev.map((t) => [t.id, t]));
      for (const t of newTrades) {
        const old = prevById.get(t.id);
        if (!old && t.status === "OPEN") {
          pushToast(
            t.side === "BUY" ? "buy" : "sell",
            `${t.side} trade opened`,
            `${t.symbol} ${t.volume} lots @ ${t.open_price?.toFixed(2)}`
          );
        } else if (old && old.status === "OPEN" && t.status === "CLOSED") {
          const p = t.profit;
          if (p == null) {
            pushToast("info", "Trade closed", `${t.symbol} ${t.side} ${t.volume} lots`);
          } else if (p >= 0) {
            pushToast("profit", `Profit +$${p.toFixed(2)} ✓`, `${t.symbol} ${t.side} ${t.volume} lots closed`);
          } else {
            pushToast("loss", `Loss -$${Math.abs(p).toFixed(2)}`, `${t.symbol} ${t.side} ${t.volume} lots closed`);
          }
        }
      }
    },
    [pushToast]
  );

  const refresh = useCallback(async () => {
    try {
      const [s, st, tr] = await Promise.all([api.getStatus(), api.getStats(), api.getTrades()]);
      setStatus(s);
      setStats(st);
      setTrades(tr);
      announceTradeChanges(tr);
      if (s.account) setLiveAccount(s.account);
      setGlobalError(null);
    } catch (err) {
      setGlobalError(err.message);
    }
  }, [announceTradeChanges]);

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, 5000);
    const disconnect = connectWebSocket((payload) => {
      if (payload.type === "tick") {
        setLiveAccount({ balance: payload.balance, equity: payload.equity, currency: "USD", leverage: 0 });
        setLiveOpenPositions(payload.open_positions || []);
        setLastSignal({
          signal: payload.signal,
          reason: payload.signal_reason,
          risk_allowed: payload.risk_allowed,
          risk_reason: payload.risk_reason,
        });
        setBasket(payload.basket || null);
        setSetup({ stage: payload.setup_stage, pending: payload.pending_setup || null });
      }
    });
    return () => {
      clearInterval(interval);
      disconnect();
    };
  }, [refresh]);

  async function handleStart(confirmReal) {
    setBusy(true);
    try {
      await api.start(confirmReal);
      await refresh();
      pushToast("info", "Bot started", "Watching the market — trades will open automatically on signals");
    } catch (err) {
      setGlobalError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleStop() {
    setBusy(true);
    try {
      await api.stop();
      await refresh();
      pushToast("info", "Bot stopped", "No new trades will be opened");
    } catch (err) {
      setGlobalError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleModeChange(mode, confirm) {
    try {
      await api.setMode(mode, confirm);
      await refresh();
    } catch (err) {
      setGlobalError(err.message);
    }
  }

  async function handleSaveSettings(newSettings) {
    try {
      await api.updateSettings(newSettings);
      await refresh();
      return true;
    } catch (err) {
      setGlobalError(err.message);
      return false;
    }
  }

  return (
    <div className="app">
      <Toasts toasts={toasts} onDismiss={dismissToast} />
      <header className="app-header">
        <h1>Forex AI Trading Bot</h1>
        <nav className="tabs">
          <button className={tab === "dashboard" ? "tab active" : "tab"} onClick={() => setTab("dashboard")}>
            Dashboard
          </button>
          <button className={tab === "backtest" ? "tab active" : "tab"} onClick={() => setTab("backtest")}>
            Backtest
          </button>
          <button className={tab === "settings" ? "tab active" : "tab"} onClick={() => setTab("settings")}>
            Settings
          </button>
        </nav>
      </header>

      {globalError && <div className="global-error">⚠ {globalError}</div>}

      <StatusBar status={status} onStart={handleStart} onStop={handleStop} onModeChange={handleModeChange} busy={busy} />

      {tab === "dashboard" && (
        <>
          <StatCards account={liveAccount || status?.account} stats={stats} liveOpenPositions={liveOpenPositions} />

          <LiveChart trades={trades} />

          <ManualTestPanel mode={status?.mode} onOrderPlaced={refresh} />

          {lastSignal && (
            <div className="panel signal-panel">
              <h3>Last Strategy Check</h3>
              <p>
                Signal: <b>{lastSignal.signal}</b> — {lastSignal.reason}
              </p>
              <p className={lastSignal.risk_allowed ? "tone-green" : "tone-red"}>
                Risk check: {lastSignal.risk_allowed ? "allowed" : "blocked"} ({lastSignal.risk_reason})
              </p>
            </div>
          )}

          {setup && (
            <div className="panel signal-panel">
              <h3>SMC Setup</h3>
              {setup.pending ? (
                <>
                  <p>
                    Planned <b>{setup.pending.side}</b> — waiting for price to reach{" "}
                    <b>{setup.pending.entry}</b>
                  </p>
                  <p className="muted">
                    Stop {setup.pending.sl} · Target {setup.pending.tp} · Reward:risk 1:{setup.pending.rr}
                  </p>
                </>
              ) : (
                <p className="muted">{setup.stage || "scanning structure"}</p>
              )}
            </div>
          )}

          {basket && (
            <div className="panel signal-panel">
              <h3>Basket</h3>
              <p>
                {basket.entries} of {basket.max_entries} entries open — combined{" "}
                <b className={basket.floating >= 0 ? "tone-green" : "tone-red"}>
                  {basket.floating >= 0 ? "+" : "-"}${Math.abs(basket.floating).toFixed(2)}
                </b>{" "}
                (closes all at +${basket.target}, group stop at -${basket.max_loss})
              </p>
              {basket.last_event && <p className="muted">Last group exit: {basket.last_event}</p>}
            </div>
          )}

          {liveOpenPositions.length > 0 && (
            <div className="panel">
              <h3>Open Positions</h3>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Symbol</th>
                      <th>Side</th>
                      <th>Volume</th>
                      <th>Open Price</th>
                      <th>Profit</th>
                    </tr>
                  </thead>
                  <tbody>
                    {liveOpenPositions.map((p) => (
                      <tr key={p.ticket}>
                        <td>{p.symbol}</td>
                        <td className={p.side === "BUY" ? "tone-green" : "tone-red"}>{p.side}</td>
                        <td>{p.volume}</td>
                        <td>{p.open_price?.toFixed(5)}</td>
                        <td className={p.profit >= 0 ? "tone-green" : "tone-red"}>${p.profit?.toFixed(2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          <EquityChart data={stats?.equity_curve} />
          <TradesTable trades={trades} />
        </>
      )}

      {tab === "backtest" && <BacktestPanel onRun={api.runBacktest} />}

      {tab === "settings" && (
        <SettingsPanel settings={status?.settings} running={status?.running} onSave={handleSaveSettings} />
      )}
    </div>
  );
}
