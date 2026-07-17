import { useState } from "react";
import ConfirmModal from "./ConfirmModal";

export default function StatusBar({ status, onStart, onStop, onModeChange, busy }) {
  const [pendingRealConfirm, setPendingRealConfirm] = useState(false);
  const [pendingStartConfirm, setPendingStartConfirm] = useState(false);

  if (!status) return null;

  const { running, mode, connected, symbol, timeframe, last_error } = status;

  function handleModeToggle() {
    const nextMode = mode === "demo" ? "real" : "demo";
    if (nextMode === "real") {
      setPendingRealConfirm(true);
    } else {
      onModeChange(nextMode, false);
    }
  }

  function handleStartClick() {
    if (mode === "real") {
      setPendingStartConfirm(true);
    } else {
      onStart(false);
    }
  }

  return (
    <div className="status-bar">
      <div className="status-left">
        <span className={`dot ${connected ? "dot-green" : "dot-gray"}`} />
        <span>{connected ? "Connected" : "Disconnected"}</span>
        <span className="sep">|</span>
        <span>
          {symbol} · {timeframe}
        </span>
        <span className="sep">|</span>
        <span className={`badge ${mode === "real" ? "badge-real" : "badge-demo"}`}>
          {mode === "real" ? "REAL MONEY" : "DEMO"}
        </span>
        {last_error && <span className="error-text">⚠ {last_error}</span>}
      </div>
      <div className="status-right">
        <button className="btn btn-ghost" disabled={running || busy} onClick={handleModeToggle}>
          Switch to {mode === "demo" ? "Real" : "Demo"}
        </button>
        {running ? (
          <button className="btn btn-danger" disabled={busy} onClick={onStop}>
            Stop Bot
          </button>
        ) : (
          <button className="btn btn-primary" disabled={busy} onClick={handleStartClick}>
            Start Bot
          </button>
        )}
      </div>

      {pendingRealConfirm && (
        <ConfirmModal
          title="Switch to REAL account?"
          message="The bot will place real orders with real money once started. Make sure your risk settings (stop loss, risk %, max daily loss) are exactly what you want before continuing."
          confirmLabel="Yes, switch to Real"
          danger
          onConfirm={() => {
            onModeChange("real", true);
            setPendingRealConfirm(false);
          }}
          onCancel={() => setPendingRealConfirm(false)}
        />
      )}

      {pendingStartConfirm && (
        <ConfirmModal
          title="Start trading on REAL account?"
          message="This will start placing real trades with real money using your current strategy and risk settings. Are you sure?"
          confirmLabel="Yes, start"
          danger
          onConfirm={() => {
            onStart(true);
            setPendingStartConfirm(false);
          }}
          onCancel={() => setPendingStartConfirm(false)}
        />
      )}
    </div>
  );
}
