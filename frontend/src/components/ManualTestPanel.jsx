import { useState } from "react";
import { api } from "../api";
import ConfirmModal from "./ConfirmModal";

export default function ManualTestPanel({ mode, onOrderPlaced }) {
  const [volume, setVolume] = useState(0.01);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [pendingSide, setPendingSide] = useState(null);

  async function placeOrder(side, confirmReal = false) {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const data = await api.testOrder(side, Number(volume), confirmReal);
      setResult(`${side} order placed — ticket ${data.ticket} @ ${data.open_price}`);
      onOrderPlaced?.();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  function handleClick(side) {
    if (mode === "real") {
      setPendingSide(side);
    } else {
      placeOrder(side, false);
    }
  }

  return (
    <div className="panel">
      <h3>Manual Test Trade</h3>
      <p className="muted">
        Places one market order right now with no strategy/signal involved and no SL/TP — purely to confirm your
        broker connection can actually execute trades. Close it yourself from MT5 or your trade history once
        confirmed.
      </p>
      <div className="settings-form">
        <label>
          Volume (lots)
          <input
            type="number"
            step="0.01"
            min="0.01"
            value={volume}
            onChange={(e) => setVolume(e.target.value)}
          />
        </label>
        <button className="btn btn-primary" disabled={busy} onClick={() => handleClick("BUY")}>
          {busy ? "Placing..." : "Test BUY"}
        </button>
        <button className="btn btn-danger" disabled={busy} onClick={() => handleClick("SELL")}>
          {busy ? "Placing..." : "Test SELL"}
        </button>
      </div>
      {result && <p className="tone-green">{result}</p>}
      {error && <p className="error-text">⚠ {error}</p>}

      {pendingSide && (
        <ConfirmModal
          title="Place a REAL order?"
          message={`This places a real ${pendingSide} order with real money right now (no SL/TP). Are you sure?`}
          confirmLabel="Yes, place it"
          danger
          onConfirm={() => {
            placeOrder(pendingSide, true);
            setPendingSide(null);
          }}
          onCancel={() => setPendingSide(null)}
        />
      )}
    </div>
  );
}
