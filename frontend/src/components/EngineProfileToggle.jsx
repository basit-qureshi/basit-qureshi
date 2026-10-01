import { useEffect, useState } from "react";
import ConfirmModal from "./ConfirmModal";

/**
 * Which engine drives the account: the guarded one, or the original.
 *
 * The toggle is two buttons rather than a switch on purpose. A switch invites
 * being flipped to see what happens; these are labelled with what each side
 * actually is, and selecting the original one asks for confirmation that lists
 * the protection it does not have. The list comes from the server
 * (`engine_profile_missing`), so the screen cannot describe the choice
 * differently from the API.
 *
 * It never claims one engine performs better. Nothing in this project measures
 * that, and the whole reason both can be run is that the comparison has not
 * been made yet.
 */
export default function EngineProfileToggle({ status, busy, onSwitch }) {
  const [catalog, setCatalog] = useState(null);
  const [pending, setPending] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    import("../api").then(({ api }) =>
      api
        .getEngineProfile()
        .then((body) => !cancelled && setCatalog(body))
        .catch(() => !cancelled && setCatalog(null))
    );
    return () => {
      cancelled = true;
    };
  }, []);

  if (!status) return null;

  const active = status.engine_profile || "guarded";
  const running = Boolean(status.running);
  const missing = status.engine_profile_missing || [];
  const notApplied = status.settings_not_applied || [];
  const profiles = catalog?.profiles || [
    { key: "guarded", label: "Guarded engine (current)", adds: [] },
    { key: "legacy", label: "Original engine (1116af1)", adds: [] },
  ];

  async function choose(key) {
    if (key === active) return;
    setError(null);
    try {
      await onSwitch(key);
    } catch (err) {
      setError(err.message);
    }
  }

  function handleClick(key) {
    if (key === "legacy") setPending(key);
    else choose(key);
  }

  const legacy = profiles.find((p) => p.key === "legacy");
  const guarded = profiles.find((p) => p.key === "guarded");

  return (
    <div className="panel engine-profile">
      <h3>Engine</h3>
      <p className="muted">
        The grid is identical in both: same levels, same spacing, same lot, same basket target,
        same next-candle gate. What changes is how much the engine refuses to assume before it
        places one — not what it trades.
      </p>

      <div className="engine-profile-choices">
        {profiles.map((p) => (
          <button
            key={p.key}
            type="button"
            className={`engine-profile-choice${p.key === active ? " is-active" : ""}`}
            disabled={busy || running || p.key === active}
            onClick={() => handleClick(p.key)}
          >
            <span className="engine-profile-name">{p.label}</span>
            {p.key === active && <span className="badge badge-demo">RUNNING</span>}
            {p.summary && <span className="engine-profile-summary">{p.summary}</span>}
          </button>
        ))}
      </div>

      {running && (
        <p className="muted">Stop the bot to change engines. A live loop cannot be swapped underneath itself.</p>
      )}

      {active === "legacy" && (
        <div className="engine-profile-warning">
          <strong>⚠ Running the original engine.</strong> It does not have:
          <ul>
            {missing.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
          {notApplied.length > 0 && (
            <p>
              These saved settings are <strong>not read</strong> by this engine:{" "}
              {notApplied.join(", ")}. They are still stored and will apply again on the guarded
              engine.
            </p>
          )}
        </div>
      )}

      {error && <p className="error-text">⚠ {error}</p>}

      {pending && (
        <ConfirmModal
          title="Run the original engine?"
          message={
            "This is the engine as it was before the capital floor, the closing-cost contract, the " +
            "symbol-valuation refusal, the liquidation policy and the owner pause existed. The grid " +
            "it places is identical — what is missing is what refuses to place one when something " +
            "needed to price it is unknown. It is the engine that produced the trade history already " +
            "in this database. Neither engine has been shown to be profitable."
          }
          confirmLabel="Yes, run the original"
          danger
          onConfirm={() => {
            choose(pending);
            setPending(null);
          }}
          onCancel={() => setPending(null)}
        />
      )}

      {/* Kept out of the warning box so it reads the same whichever side is on. */}
      {active === "guarded" && guarded?.adds?.length > 0 && (
        <details className="engine-profile-detail">
          <summary>What this engine adds over the original</summary>
          <ul>
            {guarded.adds.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
          {legacy && <p className="muted">{legacy.summary}</p>}
        </details>
      )}
    </div>
  );
}
