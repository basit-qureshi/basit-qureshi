import { useEffect, useState, useCallback } from "react";
import { api, connectWebSocket } from "./api";
import StatusBar from "./components/StatusBar";
import StatCards from "./components/StatCards";
import LiveChart from "./components/LiveChart";
import EquityChart from "./components/EquityChart";
import TradesTable from "./components/TradesTable";
import SettingsPanel from "./components/SettingsPanel";
import BacktestPanel from "./components/BacktestPanel";
import ManualTestPanel from "./components/ManualTestPanel";
import "./App.css";

export default function App() {
  const [status, setStatus] = useState(null);
  const [stats, setStats] = useState(null);
  const [trades, setTrades] = useState([]);
  const [liveAccount, setLiveAccount] = useState(null);
  const [liveOpenPositions, setLiveOpenPositions] = useState([]);
  const [lastSignal, setLastSignal] = useState(null);
  const [busy, setBusy] = useState(false);
  const [globalError, setGlobalError] = useState(null);
  const [tab, setTab] = useState("dashboard");

  const refresh = useCallback(async () => {
    try {
      const [s, st, tr] = await Promise.all([api.getStatus(), api.getStats(), api.getTrades()]);
      setStatus(s);
      setStats(st);
      setTrades(tr);
      if (s.account) setLiveAccount(s.account);
      setGlobalError(null);
    } catch (err) {
      setGlobalError(err.message);
    }
  }, []);

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
