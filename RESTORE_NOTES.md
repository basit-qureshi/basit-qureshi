# Requested restoration

Baseline: original strategy commit 5aff68a82ca3437773ec777f319cbbe40b61a5a1.
This update replaces the active trading additions from the later audit.

Implemented:
* Restore 10 buy stops, 10 sell stops, 0.01 lots, 0.30 spacing and $10 basket target.
* Restore original broker spacing calculation and no individual SL/TP.
* Remove AI and the added entry gates from the active strategy.
* Rebuild immediately after a profitable basket is confirmed closed.
* Preserve original startup, manual removal, loss exit and configured risk behavior.
* Render trade and chart times consistently in Pakistan time.
* Improve dashboard hierarchy, grid visibility and responsive layout.

Installed database compatibility is retained to avoid breaking migrated history.
The five restored grid values are applied once with a settings backup; connection,
symbol, polling and existing risk settings are preserved.

Validation uses mock execution and synthetic candles. The Windows MT5 terminal
and live broker timing were not available in this environment. Browser visual
inspection of localhost was blocked by the browser connection; frontend build,
lint, time formatting tests and component render checks provide local verification.
