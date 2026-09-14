# Trading bot audit and implementation record

## Scope and verdict

Repository reviewed at original commit 5aff68a82ca3437773ec777f319cbbe40b61a5a1.
This audit covers the grid engine, MT5 adapter, persistence, reporting, settings,
candle backtesting, dashboard and added research tools. It does not certify a
profitable strategy. No live orders were submitted during this work.

The original active strategy was a fixed two-sided pending-order grid. EMA lines
were chart decoration. No trained AI model drove its entries. A real local
classifier and optional directional entry gate are now included, but a trained
model on this user's data has not been produced or evaluated here.

## Confirmed findings and changes

| Priority | Finding | Code evidence | Implemented behavior |
| --- | --- | --- | --- |
| Critical | Failed position/order reads looked like empty account state | MT5Broker.get_open_positions and get_pending_orders | State failures raise; no fresh grid from unknown state |
| Critical | Stop cancelled monitoring while broker exposure remained | GridEngine.stop and _loop | Stop requests closure; remaining orders and positions are retried |
| Critical | A failed close could be abandoned after price moved away from its trigger | GridEngine._close_everything and _tick | Closing intent persists until broker confirms flat, including across restart |
| High | History/cost availability could prevent emergency exits | MT5Broker position costs and engine accounting order | Unknown costs are flagged; history failure blocks new grids while basket exits remain enabled |
| High | Position cap acted only after fills | GridEngine._build_grid | Reject total grid levels above the cap before placing any orders |
| High | Daily loss was an in-memory counter | GridEngine._check_risk_limits and db.daily_totals | Scoped settled daily results plus current floating P&L; persistent drawdown peak and halts |
| High | Demo/real label did not verify the account | MT5Broker.get_account_info and GridEngine.start | Validate actual MT5 mode, USD currency and hedging support; real start disabled by default |
| High | Individual positions lacked broker protection if Python stopped | MT5Broker.place_pending_order | New pending orders include a disaster SL, normally 5.00 price units away |
| High | Partial grids were left running after order rejection | GridEngine._build_grid | Partial build requests flattening and a persistent halt |
| High | Restart reconciliation and statistics mixed identities | db migration, GridEngine ledger and API statistics | Account/symbol/magic scoping, duplicate retention and unique active records |
| High | Trades opened/closed while offline could disappear from daily risk accounting | MT5 history synchronization | Import verified opening-magic positions and full deal costs; use stable position IDs |
| Medium | Basket trigger did not include reported costs | Position.net_profit and MT5 deal costs | Include known commission, fee and swap; final settlement remains separately verified |
| Medium | Candle backtest counted later fills in earlier target calculations | backtester chronological event loop | Evaluate each fill/exit in sequence, report ending floating exposure |
| Medium | Old environment fields looked effective but did not control this engine | Config and BotManager settings mapping | README names effective GRID_* settings and runtime override precedence |
| Medium | Manual test endpoint submitted unprotected orders | API test-order and dashboard | Endpoint disabled and control removed |
| Medium | Server exposed an unnecessary network listener and reload process | run.py | Localhost binding and one process |
| Medium | No trained AI despite project label | app/ai | Causal features, softmax training, purged holdout, availability timestamp and optional filter |
| Medium | Equity display could obscure realized versus floating results | API/replay outputs and documentation | Replay explicitly reports ending equity and remaining exposure; historical chart represents realized bot P&L |

## Screenshot observations

The dashboard screenshot shows balance 76.16 USD, equity 68.62 USD, daily gross
profit 102.62, daily gross loss 124.01 and daily net loss 21.39. Its displayed
profit factor is 0.96 over the displayed history, which is not evidence of a
positive realized edge. These observations are screenshots, not an independently
reconciled broker performance report. The two application screenshots show
different moments; their equity values should not be treated as simultaneous.

The MT5 screenshot shows opposing positions and pending orders. Equal directional
volume can neutralize further price sensitivity while preserving a loss. It
does not remove spread, swap or margin requirements. No trade ownership can be
proven from this screenshot alone.

A credential was visible in the uploaded environment screenshot. Rotate that
password. It has not been copied into code or this report.

## Validation added

GitHub Actions compiles Python, runs pytest, lints the frontend and builds the
production frontend. Regression tests exercise restart identity, loss persistence,
close retries, accounting outages, pending-order price/SL rounding, costs,
unsettled result recovery, CSV ownership ambiguity, causal features, model
availability, replay isolation and chronological order execution.

Tests use deterministic doubles and synthetic market paths. The GitHub runner
does not execute a Windows MT5 terminal and cannot establish broker fill quality.
The workflow log is the evidence for the actual passing test count.

## AI and replay interpretation

The model uses return, volatility, range and EMA-trend features on closed M1
candles. It predicts SELL, HOLD or BUY over a short horizon. Features and
standardization exclude future observations. A label-horizon purge separates
training from holdout; the artifact is unavailable until all evaluation labels
would have been known.

Eligibility compares a directional proxy with a baseline. This is not the grid's
expected profit and model probabilities are not calibrated win probabilities.
Default shadow mode reports its opinion without changing orders. Filter mode
requires an eligible, fresh, matching model and sufficient confidence, and may
block all entries. Missing data or model errors do not produce invented signals.

Recorded Bid/Ask replay uses the same GridEngine with a virtual broker and a
temporary database. It includes gap fills, polling and disaster stops, but omits
commission, swap, latency, rejected orders, broker-specific stop rules, margin
liquidation and other live execution effects. The OHLC dashboard backtest also
uses an assumed intrabar path and proxy market data. Neither is live validation.

## Outstanding evidence

The supplied CSV and video could not be read with the available file tools.
Their contents were not analyzed. Manual trades have not been fully separated.
The CSV helper groups available magic values, but does not infer ownership from
trade appearance; exits with different magic require a position-ID join.

The user's current runtime settings, symbol contract/tick specifications and
Windows execution environment remain unverified. The default settings are not
suitable sizing advice for the displayed balance. Broker stops may slip and
cannot guarantee a money loss ceiling.

Before evaluating readiness, obtain correctly attributed deal history, measured
costs, unseen broker ticks and forward demo results. Compare the same grid with
and without AI across trends, reversals and spread expansion. Inspect drawdown,
floating exposure, turnover and execution failures as well as net return.
Changing parameters or adding a classifier alone cannot guarantee profitability.
