# What the TWP reference archive actually contains

Read-only inspection of `TWP_SET_FILES_1.14.zip`, extracted outside the
repository. Nothing in it was executed: the archive ships `Check GMT offset.ex4`
and `Check GMT offset.ex5`, which are compiled MetaTrader binaries, and compiled
attachments are not run here. No source was recovered, no licence accepted, and
nothing in the archive was treated as an instruction.

The archive is a different product with a different strategy. It is used here
only as evidence about **how a published system bounds risk and chooses when to
trade** — not as something to copy into this bot.

## Inventory

266 files. 104 `.png`, 80 `.txt`, 48 `.set`, 28 `.html`, 4 `.pdf`, 1 `.ex5`,
1 `.ex4`. The 48 presets are 24 for MT5 and 24 for MT4, split into
`Sets/low-frequency/` and `Sets/regular (most popular)/`.

The `.set` files are **UTF-16LE**. A plain ASCII `grep` over them matches
nothing, which is silent rather than noisy — every count below was taken after
converting to UTF-8. Values carry MT5's optimiser suffix
`value||start||step||stop||optimise_flag`; only the part before the first `||`
is the setting in use.

## Evidence matrix

| Claim | Where it comes from | What the files actually say |
| --- | --- | --- |
| One trade per day, one at a time | every MT5 preset | `iMaxTradesDaily=1` in 24 of 24, `iMaxTradesAtOnce=1` in 24 of 24 |
| Fixed daily and overall drawdown ceiling | every MT5 preset | `iDailyDrawdown=4` in 23 of 24 (one outlier at 100), `iOverallDrawdown=8` in 23 of 24 (one at 10) |
| Risk is sized against a capped capital base, not the live balance | every MT5 preset | `iMaxCapital=10000` in 18, `11000` in 3, `111000` in 3, `10000.0` in 1 |
| Entry is confined to a short daily window after a measured range | every MT5 preset + `!README.txt` per folder | `iRangeMinutes` 10–150, then `iTradeSessionMinutes` 20–360 |
| "Advanced risk management" ships **off** | every MT5 preset | `iUseAdvancedRisk=false` in 24 of 24 |
| The archive's "grid" is not a 10+10 pending grid | presets + README | `iUseGrid=true` in 20 of 24, but `iMaxGridLevels=0` in all 11 files that set it, and `iMaxTradesAtOnce=1` everywhere |
| Grid replaces the stop loss rather than adding to it | `MT5/Sets/low-frequency/g_XAUUSD Asia/!README.txt` | "Instead of a fixed stop loss, position management is handled through grid logic... If you want to enforce strict risk limits, you can enable the Advanced Risk Management feature" |
| Reported results assume costs this bot has not measured | `.../Report Tester/Backtest settings.txt` | "Commission: 2.75$ per lot ($5.5 round turn)", "Modelling: 1 minute OHLC", "Leverage: 1:500", "Latency: 1000ms" |

### `iLotsMode` — resolved by reconciliation, not by guessing

Two modes appear, and each is pinned by a README that states the same number in
words:

- `iLotsMode=2` is **capital per lot**. `MT5/Sets/low-frequency/_XAUUSD NYSE`
  has `iLots=15000`; its README says "1 lot for each $15,000... [Your capital] /
  15,000". `low-frequency/g_XAUUSD Asia` has `iLots=100000` and says
  "[Your capital] / 100,000". Used by 9 of 24.
- `iLotsMode=1` is **percent risked per trade**. `low-frequency/_XAUUSD America`
  has `iLots=10`; its README says "Risk management is set to 10% per trade".
  Used by 15 of 24.

### A README that its own preset does not support

`MT5/Sets/regular (most popular)/_XAUUSD Asia/!README.txt` states
"Risk management is set to 0.12% per trade". The preset beside it,
`XAUUSD Breakout Asia.set`, carries `iLotsMode=1` with `iLots=0.5` — under the
mapping above, **0.5% per trade, roughly four times the advertised figure**.
The value 0.12 does occur in the archive, but in a different pair:
`regular (most popular)/g_GBPUSD Asia/GBPUSD FVG S&D Asia.set`. The most likely
explanation is a README copied between folders.

This is recorded because it is the reason preset numbers are not adopted on the
strength of a description. It is not an accusation about the product.

## Still unknown, and left unknown

- `iTradeSession` is an enum with observed values 0–4. Folder names imply Asia /
  Europe / London / NYSE / America, but no file in the archive maps the integers
  to sessions, so the mapping is not assumed.
- `iMaxGridLevels=0` appears in 11 presets. Whether 0 means "unlimited",
  "disabled" or "use the EA default" is not documented anywhere in the archive.
- `iEntryMode`, `iSLMode`, `iTPMode`, `iOpenFilter` and `DstRegion` are likewise
  undocumented enums.
- Every performance figure in the archive comes from **the vendor's own strategy
  tester reports**, on an AAAFx demo account with a static GMT offset. None of
  it was independently reproduced here, and none of it says anything about what
  this bot will do.

## What is worth taking, and what is not

Worth taking — and all of it is about bounding loss, not about predicting price:

1. **A hard daily drawdown ceiling and a separate overall one.** Two numbers,
   both always on, in every single preset. This bot has the equivalent controls;
   the owner has not yet set the numbers.
2. **A capped capital base for sizing.** Risk computed against a fixed figure
   rather than a balance that grows after a good run.
3. **One position at a time, one trade per day.** The opposite of a 20-order
   grid. It is the clearest reason a published system can afford to sit out.
4. **Entry confined to a measured window.** Not a signal — a schedule.

Not worth taking, and deliberately not implemented:

- Its entries (opening-range breakout, retrace, FVG, supply/demand). The locked
  strategy here uses no indicators, signals, filters or trend rules, and none
  were added.
- Trailing stops, partial closes, per-trade SL/TP. All of these are individual
  order management, which the locked strategy excludes.
- Any of its preset numbers as settings for this bot. Different instrument
  behaviour, different broker, different cost model, different strategy.
