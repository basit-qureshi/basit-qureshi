import { useState } from "react";
import ConfirmModal from "./ConfirmModal";

export default function StatusBar({
  status, onStart, onStop, onModeChange, onPauseEntries, onResumeEntries, onCloseAndPause, busy,
}) {
  const [pendingRealConfirm, setPendingRealConfirm] = useState(false);
  const [pendingStartConfirm, setPendingStartConfirm] = useState(false);
  const [pendingStopConfirm, setPendingStopConfirm] = useState(false);
  const [pendingCloseConfirm, setPendingCloseConfirm] = useState(false);

  if (!status) return null;

  const {
    running, mode, connected, symbol, timeframe, last_error, strategy_name, daily_target_hit,
    entries_paused, pause_reason, close_intent, persistence_error, liquidation_policy,
    engine_profile,
  } = status;

  // Named by the engine from the strategy object it is actually holding, not
  // from the settings dict — so a saved setting that quietly disagrees with the
  // dashboard shows up here instead of only in the shape of the trades.
  const STRATEGY_LABELS = { GridEngine: "Grid", LegacyGridEngine: "Grid" };
  const strategyLabel = STRATEGY_LABELS[strategy_name] || strategy_name;
  // The grid is the same either way, so this is NOT a second strategy label.
  // It says which engine is in front of that grid, and it sits on the main bar
  // rather than only in Settings because the difference is what refuses a trade.
  const onLegacy = engine_profile === "legacy";

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
        {strategyLabel && (
          <>
            <span>{strategyLabel}</span>
            <span className="sep">|</span>
          </>
        )}
        <span className={`badge ${mode === "real" ? "badge-real" : "badge-demo"}`}>
          {mode === "real" ? "REAL MONEY" : "DEMO"}
        </span>
        {onLegacy && (
          <span className="badge badge-real" title="The engine as it was at commit 1116af1: no capital floor, no closing-cost contract, no liquidation policy, no owner pause.">
            ORIGINAL ENGINE
          </span>
        )}
        {daily_target_hit && <span className="tone-green">✓ Daily target reached — halted for this broker day</span>}
        {entries_paused && (
          <span className="tone-amber">⏸ Entries paused{pause_reason ? ` — ${pause_reason}` : ""}. Open positions are still managed.</span>
        )}
        {/* A standing instruction only the owner can lift. It outlives the close
            that satisfied it, so it needs its own line: without this, a bot
            that keeps flattening every late fill looks broken rather than
            obedient. */}
        {liquidation_policy && (
          <span className="error-text">
            🛑 Holding exposure at zero after {liquidation_policy.cause}
            {liquidation_policy.cleanups > 0 && ` — ${liquidation_policy.cleanups} cleanup(s) since`}
            . Only Resume entries clears this.
          </span>
        )}
        {close_intent && close_intent.state !== "DONE" && (
          <span className="error-text">
            ⏳ Closing ({close_intent.state}, attempt {close_intent.attempts}) — {close_intent.reason}
          </span>
        )}
        {persistence_error && <span className="error-text">⚠ {persistence_error}</span>}
        {last_error && <span className="error-text">⚠ {last_error}</span>}
      </div>
      <div className="status-right">
        <button className="btn btn-ghost" disabled={running || busy} onClick={handleModeToggle}>
          Switch to {mode === "demo" ? "Real" : "Demo"}
        </button>
        {/* Three distinct actions, because they do three different things.
            Pause keeps protecting. Close flattens this bot's own exposure.
            Stop ends the management loop and protects nothing after that. */}
        {/* The original engine has no owner pause, so the buttons are not shown
            on it. Showing one that always refuses would be worse than its
            absence: it would suggest protection that is not there. */}
        {!onLegacy && running && !entries_paused && (
          <button className="btn btn-ghost" disabled={busy} onClick={onPauseEntries}>
            Pause entries
          </button>
        )}
        {!onLegacy && running && entries_paused && (
          <button className="btn btn-ghost" disabled={busy} onClick={onResumeEntries}>
            Resume entries
          </button>
        )}
        <button className="btn btn-ghost" disabled={busy} onClick={() => setPendingCloseConfirm(true)}>
          Close positions
        </button>
        {running ? (
          <button className="btn btn-danger" disabled={busy} onClick={() => setPendingStopConfirm(true)}>
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

      {pendingStopConfirm && (
        <ConfirmModal
          title="Stop the management loop?"
          message={
            "Stop ends the loop that watches this bot's positions. After it stops, the basket target, " +
            "the basket stop, the daily loss limit and the drawdown limit are NO LONGER CHECKED, and any " +
            "open positions stay live at the broker with nothing closing them. " +
            "To keep protection running while opening nothing new, use Pause entries. " +
            "To end the exposure, use Close positions first."
          }
          confirmLabel="Yes, stop watching"
          danger
          onConfirm={() => {
            onStop();
            setPendingStopConfirm(false);
          }}
          onCancel={() => setPendingStopConfirm(false)}
        />
      )}

      {pendingCloseConfirm && (
        <ConfirmModal
          title="Close this bot's positions?"
          message={
            "This closes only positions and orders carrying this bot's magic number. Manual trades and " +
            "other programs are not touched. " +
            (onLegacy
              ? "The original engine has no pause that keeps entries shut while the loop runs, so the " +
                "management loop is STOPPED as part of this. Pressing Start again builds a fresh grid " +
                "on the next candle."
              : "It stays active until the broker confirms nothing is left.")
          }
          confirmLabel={onLegacy ? "Yes, close and stop" : "Yes, close and pause"}
          danger
          onConfirm={() => {
            onCloseAndPause();
            setPendingCloseConfirm(false);
          }}
          onCancel={() => setPendingCloseConfirm(false)}
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
