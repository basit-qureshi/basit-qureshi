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
import PakistanClock from "./components/PakistanClock";
import GridPanel from "./components/GridPanel";
import OpenTradesPanel from "./components/OpenTradesPanel";
import "./App.css";

const DEFAULT_FILTERS = {
  date_from: "",
  date_to: "",
  status: "ALL",
  side: "ALL",
  result: "all",
  search: "",
  page_size: 50,
};

export default function App() {
  const [status, setStatus] = useState(null);
  const [stats, setStats] = useState(null);
  const [tradePage, setTradePage] = useState(null);
  const [tradingDays, setTradingDays] = useState(null);
  const [filters, setFilters] = useState(DEFAULT_FILTERS);
  const [page, setPage] = useState(1);
  const [tradesLoading, setTradesLoading] = useState(true);
  const [openTrades, setOpenTrades] = useState(null);
  const [liveAccount, setLiveAccount] = useState(null);
  const [liveOpenPositions, setLiveOpenPositions] = useState(null);
  const [grid, setGrid] = useState(null);
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

  // The filters and page live here and go to the server, so the rows on screen
  // and the summary above them always come from the same query.
  const loadTrades = useCallback(async () => {
    setTradesLoading(true);
    try {
      const body = await api.getTrades({ ...filters, page });
      setTradePage(body);
      announceTradeChanges(body.items);
      // The server clamps a page past the end; follow it so the controls and
      // the rows agree about which page is showing.
      if (body.page !== page) setPage(body.page);
      return body;
    } catch (err) {
      setGlobalError(err.message);
      return null;
    } finally {
      setTradesLoading(false);
    }
  }, [filters, page, announceTradeChanges]);

  const refresh = useCallback(async () => {
    try {
      const [s, st, open, days] = await Promise.all([
        api.getStatus(),
        api.getStats(),
        api.getOpenTrades().catch(() => null),
        api.getTradingDays().catch(() => null),
      ]);
      setStatus(s);
      setStats(st);
      if (open) setOpenTrades(open);
      if (days) setTradingDays(days);
      if (s.account) setLiveAccount(s.account);
      setGlobalError(null);
    } catch (err) {
      setGlobalError(err.message);
    }
  }, []);

  // Re-runs whenever a filter or the page changes, and on the poll below.
  useEffect(() => {
    loadTrades();
  }, [loadTrades]);

  // loadTrades changes identity on every filter and page change. Reaching it
  // through a ref keeps the poll and the WebSocket effect below stable, so
  // changing a filter does not tear down and re-open the live connection.
  const loadTradesRef = useRef(loadTrades);
  useEffect(() => {
    loadTradesRef.current = loadTrades;
  }, [loadTrades]);

  useEffect(() => {
    refresh();
    const interval = setInterval(() => {
      refresh();
      loadTradesRef.current();
    }, 5000);
    const disconnect = connectWebSocket((payload) => {
      if (payload.type === "tick") {
        setLiveAccount({ balance: payload.balance, equity: payload.equity, currency: "USD", leverage: 0 });
        setLiveOpenPositions(payload.open_positions || []);
        setGrid(payload.grid || null);
      }
    });
    return () => {
      clearInterval(interval);
      disconnect();
    };
  }, [refresh]);

  // Changing a filter always returns to page 1: staying on page 7 of a
  // selection that now has two pages would show an empty screen.
  const handleFilterChange = useCallback((next) => {
    setFilters(next);
    setPage(1);
  }, []);

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

  async function handleClearHalt() {
    try {
      const result = await api.clearHalt();
      pushToast("info", "Risk halt cleared", result.message);
      await refresh();
    } catch (err) {
      // The refusal text explains what is still open, which is the useful part.
      pushToast("loss", "Halt not cleared", err.message);
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
        <div className="brand"><div className="brand-mark">G</div><div><span className="eyebrow">TRADING WORKSPACE</span><h1>Gold Grid</h1></div></div>
        <PakistanClock />
        <nav className="tabs" aria-label="Main navigation">
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
          <StatCards account={liveAccount} stats={stats} liveOpenPositions={liveOpenPositions} />

          {/* Live exposure sits above the chart. What the account is holding
              right now is the thing worth seeing first, without scrolling. */}
          <OpenTradesPanel data={openTrades} />

          <div className="dashboard-workspace">
            <LiveChart trades={tradePage?.items || []} />
            <GridPanel grid={grid} status={status} onClearHalt={handleClearHalt} />
          </div>

          <EquityChart data={stats?.equity_curve} title="Realized trade P&L" />
          <TradesTable
            page={tradePage}
            filters={filters}
            days={tradingDays}
            loading={tradesLoading}
            onFilterChange={handleFilterChange}
            onPageChange={setPage}
          />
          <details className="manual-tools"><summary>Manual connection test</summary><ManualTestPanel mode={status?.mode} onOrderPlaced={refresh} /></details>
        </>
      )}

      {tab === "backtest" && <BacktestPanel onRun={api.runBacktest} />}

      {tab === "settings" && (
        <SettingsPanel settings={status?.settings} running={status?.running} onSave={handleSaveSettings} />
      )}
    </div>
  );
}
