# View Engine quant sidecar

Python service that the Rust backend calls over HTTP. It returns market data, indicators, correlations,
the economic calendar, FX sessions, options positioning, Dow body-close structure and gradient-boosted
probabilities as JSON. The output is meant to go into LLM trading-analyst prompts. It uses free data only.
**It is an analysis tool that produces statistics and probabilities. It is not financial advice.**

## Run

```bash
cd quant
uv sync                      # Python 3.12 venv from uv.lock
uv run uvicorn app.main:app --host 127.0.0.1 --port 8090
uv run pytest                # offline unit tests
```

On macOS, LightGBM needs the OpenMP runtime: `brew install libomp`. If `lightgbm` can't be imported,
the models fall back to scikit-learn `HistGradientBoosting*`. `/health` reports which `engine` is active.

Optional: set `OANDA_API_TOKEN` to use OANDA v20 candles and pricing for FX pairs and XAU/XAG. A free practice
account is enough. Also set `OANDA_ENV=practice|live` (default `practice`). You can set `OANDA_ACCOUNT_ID` too;
otherwise the first account is discovered automatically. The token is never logged or echoed.

## Endpoints

Every response is JSON and includes `source` and `as_of` (UTC ISO), plus `delayed_minutes` where it applies.
A bad symbol, interval or parameter returns 400 `{error, kind}`. Missing data returns 404. Nothing returns a stack trace.

| Endpoint | Purpose |
|---|---|
| `GET /health` | status, version, ML engine, whether OANDA is enabled |
| `GET /symbols` | canonical symbols and their metadata (aliases, Yahoo/stooq/FRED tickers, currencies, options proxy, related basket) |
| `GET /ohlc?symbol&interval=1d&lookback=200` | candles `{t,o,h,l,c,v}`, last, change_pct, delay estimate, age of the last bar |
| `GET /price?symbol` | latest price (Yahoo 1m, or OANDA pricing) |
| `GET /analysis?symbol&interval=1d` | ATR14, RSI14, SMA20/50/200, EMA21, MACD, Bollinger, realized vol, trend regime, pivots, swings, S/R zones, 52w range |
| `GET /correlation?symbols=A,B,C&interval=1d&window=60` | correlation matrix. For each symbol vs the first: rolling corr 20/60/250, 1y average with a regime-change flag, lead-lag, beta |
| `GET /calendar?currencies=USD,JPY&impact=High,Medium&days=7&past_hours=24` | ForexFactory events with `minutes_until` and status `upcoming`/`recent` |
| `GET /sessions` | Sydney/Tokyo/London/New York open and close times in UTC (DST-aware), active sessions, overlaps, next open, whether FX is open |
| `GET /options?symbol&expiries=3` | OI/volume, P/C ratios, max pain, call/put walls, ATM IV, skew (avg IV of 3–7% OTM puts minus calls), dealer GEX per strike, gamma flip. Proxy levels are mapped back to the underlying, with a caveat |
| `GET /predict?symbol&interval=1d&horizon=1` | LightGBM P(up) (raw and walk-forward-calibrated), return quantiles q10/q50/q90, price range, walk-forward validation, top features |
| `GET /structure?symbol&interval=1h&lookback=300&left=2&right=2` | Dow structure on candle **bodies**: HH/HL/LH/LL swings, trend, BOS vs wick sweeps (with sessions), consolidation box (holding/broken/failed), impulses, previous-session body/wick H/L, setup summary with triggers and stops |
| `GET /signals?symbol&intervals=4h,1h` | signals fired by the latest **closed** candle on each interval (bos, box breakout with an impulse flag, failed breakout, sweep), multi-timeframe alignment, stop and targets with R multiples, `continuation_probability`. Deduplicate on `id` |
| `GET /structure/model?symbol&interval=1h&k=24` | structure-event classifier: event counts, base hit rate per type, walk-forward stats |
| `GET /backtest?symbol&interval=1h&k=24&target_r=1.0&cost_r=0.05` | backtest of the body-close rules over all available history, with the same events, entries and stops as `/signals`. Returns overall stats (win rate with 95% Wilson CI, breakeven win rate, expectancy in R net of `cost_r`, profit factor, max drawdown, losing streak), a `verdict`, breakdowns by rule, session, higher-timeframe alignment, impulse, weekday and box width, an equity curve in cumulative R (≤200 points) and the 20 most recent trades |
| `GET /backtest/matrix?symbols=USDJPY,EURUSD,XAUUSD&intervals=4h,1h` | one row per symbol and interval: n_trades, win_rate, expectancy_r, verdict |
| `GET /retest?symbol=XAUUSD&interval=15m&level_interval=1h&pip=0.1&sl_pips=25&tp_pips=75&max_wait=48&k=64` | retest lab for break → retest → continue. Levels are swing body lines from `level_interval` and (with `levels=both`) the entry interval. Reports three entry variants (`touch`, `reject_close`, `next_open`) with fixed-pip stops and targets, how far wicks go beyond the level (percentiles, and the share of eventual winners each stop size keeps), an SL×TP grid with an overfitting warning, the `active` lines near price, and `markdown`. Other params: `tol_pips`, `spread_pips`, `levels=htf\|both`, `grid_entry`, `history_days` |
| `GET /session-story?symbol=XAUUSD&interval=15m&days=3&pip=0.1` | how each session (Asia, London, New York) acted on the last N trading days: open/close, body and wick extremes, range and net move in pips, break or sweep of the previous session's body high/low, lines broken/retested/rejected, impulse count, a character label. Plus `now` (current session, lines above/below with status, untouched session extremes, calendar blackouts, if-then `next_actions`) and `markdown` |
| `GET /levels?symbol&intervals=4h,1h,15m&pip=&history_days=` | level reactions for any symbol. Swing body levels of each interval are merged into zones (within 0.25 ATR of the lowest interval) that remember which timeframes formed them. Every interaction is classified (`rejection`, `sweep`, `break`, `retest_hold`, `retest_fail`) with session, wick depth and follow-through. Returns the top zones above and below price with score and reaction history, hold-vs-break statistics by confluence, prior respected touches, role flip, session and approach (Wilson CIs, n, and the same statistic on shuffled candles), the multi-timeframe stack with the forming higher-timeframe candle, the odds for a touch now, and `markdown` |
| `GET /mtf?symbol&intervals=4h,1h,15m,5m&pip=&sl_pips=25&tp_pips=50&tp2_pips=100&fakeout_max=3` | top-down read. Per timeframe: `regime` (consolidation, impulse or trend by body swings), the `box` with body edges, wick extremes and where price sits, `last_impulse` / `lead_in_impulse`, wick `sweeps`, body-close `fakeouts`, box `history`, and what must print to continue up or down. Combined: `stack` (context → setup → trigger with a one-paragraph reading), `playbook` (mode `range`, `break_retest`, `trend_pullback` or `stand_aside`, with zones, stops, targets, `reasons` and an ordered `cases` list of sell / buy / hold), `prior_box_edges`, measured `odds` with a shuffled-candle baseline, `data_note`, `needs` and `markdown` |
| `GET /history/status?symbol` | how many Dukascopy days are cached on disk for a symbol, and whether the feed is rate-limiting |
| `GET /snapshot?symbol=USDJPY&timeframes=1d,4h,1h&horizon=1d` | combined pack of all of the above, plus `markdown`, a dense LLM-ready summary of about 1–1.5k tokens. A section that fails shows up under `errors` and doesn't fail the rest |

Symbols are matched loosely. Case, spaces and slashes are ignored, and aliases work, for example `usd/jpy`, `gold`,
`xau/usd`, `dollar index`, `us 10y`, `s&p 500` and `nasdaq`. Any 6-letter ISO FX pair works, and other stock or ETF
tickers are passed through to Yahoo. Intervals: `5m 15m 30m 1h 4h 1d 1wk 1mo`.

## Data sources and delays

| Data | Source | Typical delay / notes |
|---|---|---|
| FX spot | Yahoo `XXXYYY=X` via yfinance | ~real-time, indicative quotes. **Daily FX bars are rebuilt from 1h bars with a 17:00 New York cut-off.** Yahoo's own FX daily bars close at about 00:00 London, so they are shifted by one day, and most have open==close. History older than about 2 years falls back to Yahoo's daily bars, re-aligned |
| Gold / silver | `GC=F` / `SI=F` front-month futures | ~10 min. The futures sit above spot by the carry |
| DXY | `DX-Y.NYB` | ~30 min |
| US10Y | `^TNX` (yield in %), with FRED `DGS10` as fallback | ~15 min |
| US2Y | FRED `DGS2` (daily constant-maturity yield) | ~1 business day. Yahoo `2YY=F` is sparse and noisy, so it is only a fallback, and **there are no intraday bars** |
| Indices / VIX / WTI | `^GSPC ^NDX ^DJI ^N225 ^VIX CL=F` | US cash indices real-time; Nikkei ~20 min; futures ~10 min |
| ETFs / stocks / options | Yahoo / yfinance | quotes real-time. Option OI is from the prior close; IV comes from Yahoo |
| Calendar | `nfs.faireconomy.media/ff_calendar_{thisweek,nextweek}.json` | rate-limited, so results are cached for 30 min in memory and on disk (`quant/.cache`). An upstream error is retried after 5 min, and until then the last good copy is served for up to 36h. Next week's feed often returns 404 until late in the week. The feed has **no actuals** |
| stooq | daily fallback | currently answers non-browsers with a JS proof-of-work challenge. That challenge is **not** bypassed, so in practice this fallback is inactive |
| OANDA (optional) | v20 REST, mid candles | real-time. Daily candles are aligned to 17:00 NY, the same convention as above |

Cache TTLs: intraday prices 60s, daily 30 min, options 5 min, calendar 30 min, trained models 6h.

Bar conventions: intraday bars are stamped at their **open** time (UTC). Daily bars are stamped 00:00 UTC on the trade date.
H4 bars are aligned to the 17:00 New York close (DST-aware), the same as OANDA/MT4. FX weekend bars are dropped.
Structure and signals only use **closed** candles. A candle's close time is its open plus the interval for intraday
bars, 17:00 NY for FX, metals and futures daily bars, and 16:00 NY for US cash daily bars.

## Models and caveats

- `/predict` adds features step by step: lagged returns, realized vol, ATR%, RSI, distance to SMAs, range position,
  day of week and hour, and returns of the related basket **lagged one bar** so their different close times can't leak.
  Validation is an expanding-window walk-forward with 5 folds over the last 50% of the data and an h-bar purge gap.
  `prob_up` is Platt-calibrated on the out-of-sample folds, so a model with no skill collapses to the base rate.
  `signal_quality` states plainly whether the model beat the baseline out of sample. In live tests on major FX and gold at a
  1-day horizon it **didn't** (Brier skill score below 0). Read the output as "no edge" rather than as a forecast.
- The structure model labels each historical BOS or box-breakout event of the symbol and interval with whether +1R
  was reached before the protected-swing stop (body extreme ± 0.2 ATR) within K candles. If both are touched in the same
  candle, it counts as a loss. Spread and slippage are ignored. It reports base hit rates, typically about 0.40–0.45 at 1R,
  which is useful context even when the classifier has no edge.
- Options on FX pairs come from currency-ETF proxies (FXY, FXE, …). Their OI is tiny compared with the OTC FX options
  market. GEX uses the "dealers long calls / short puts" convention and is not observed positioning. Inverse proxies
  map call walls **below** the pair. TLT→10Y uses a duration approximation.
- Yahoo is an unofficial source and can rate-limit or change without notice. Sources fall back in this order:
  yfinance → raw Yahoo chart API → FRED/stooq.
- `/backtest` enters at the signal candle's close, puts the stop at the protected swing body ± 0.2 ATR and the target
  at `target_r` × risk. A trade is a win if the target is hit first within `k` candles, a loss if the stop is hit first
  (both in one candle counts as a loss), and otherwise a timeout marked to market in R. Rules that fire on the same
  candle in the same direction are one trade. `verdict` is `edge` only when the lower bound of the 95% CI is above the
  breakeven win rate, expectancy is positive and there are at least 100 trades. Trades overlap in time, so the CI is
  optimistic, and the sub-group verdicts are many comparisons, so some will look good by chance. Intraday history is
  limited to about 2 years (Yahoo). In live runs on USDJPY, EURUSD and XAUUSD at 1h and 4h, the verdict was
  `no proven edge` for every pair.

## Replay mode

`/snapshot`, `/analysis`, `/structure`, `/signals`, `/session-story`, `/retest`, `/backtest`, `/predict`, `/correlation`,
`/sessions` and `/price` accept `as_of=<UTC ISO timestamp>`. Every series is cut to the candles that had **closed** by
then before anything is computed, and "now", the current session and minutes-until are all relative to it.
The response carries `replay: true` and `replay_as_of` (the requested time); `as_of` stays the timestamp of the data.
`/price?as_of=` returns the last close at or before that time. Models train only on data up to `as_of`.
Option chains are omitted in replay, and the calendar is marked unavailable for weeks the free feed no longer holds.
A candle that was still forming at `as_of` is not reconstructed, so `forming_candle` is null.

`/ohlc` also accepts `start` and `end` (UTC ISO, candle start times, inclusive) to fetch the candles between two
past timestamps, for example to score a replayed plan.

## Deep history (Dukascopy) and gold in spot terms

- `app/dukascopy.py` reads Dukascopy's public per-day M1 bid candle files (LZMA, 24-byte big-endian records,
  verified against live files) for FX majors, JPY crosses, XAUUSD and XAGUSD, and caches each day under
  `quant/.cache/dukascopy/`. `/retest` and `/backtest` take `history_days` (default 365 for those symbols).
- The feed rate-limits hard: in testing it returned HTTP 429 after three files and stayed limited for over half an hour.
  The adapter doesn't work around that. API requests never wait on the feed; a background thread fills the cache
  newest day first, spaced 5 s apart, and stops for 15 minutes to 2 hours on a 429. Deep history is used only once the
  cached days reach further back than Yahoo does (about 60 days for 5m/15m/30m). Until then responses use Yahoo and
  say so under `history`.
- Gold and silver candles are COMEX futures moved to spot terms. The basis (futures minus spot) is taken live from
  gold-api.com. When at least two Dukascopy spot days are cached, the basis becomes time-varying: it is measured on
  those days, interpolated between them and shifted to match the live basis now. Measured live, the gold basis was
  about 40 on 2026-09-15 and about 30 on 2026-10-02, so a single constant basis misplaces levels from three weeks ago
  by roughly $10–20. Dukascopy's bid was also about $6–9 below the gold-api/OANDA spot quote at the same moment.

## Level reactions (`/levels`)

- A zone is a swing body level ± 0.25 ATR (lowest interval). A later level within that distance joins the zone, which is
  how timeframe confluence is recorded, with the time each timeframe joined. A level lives 200 bars of its own interval
  (counted in trading time), and a zone is retired after 4 body closes through it; a new swing at the same price then
  starts a fresh zone.
- A touch is a **hold** when price moves 1 ATR away before any candle closes through the zone, and a **break** when a
  candle body closes through it; it has 16 candles to decide. A hold with a wick beyond the zone is a `sweep`, the first
  hold after a break is a `retest_hold` (support ↔ resistance flip), and a close back through is a `retest_fail`.
- The conditions of a touch (confluence, prior respected touches, flips, session, approach) are the ones known when the
  touch started. "Approach" uses only the candles before the touch candle.
- The hold and break thresholds are not symmetric, so a hold rate away from 50% proves nothing by itself. Each bucket
  is therefore compared with the same rules run on shuffled candles (same candles, random order within each hour of
  the day). Only buckets flagged `beats_null` behave differently from random price action. In live runs on about ten
  weeks of 15m data, XAUUSD lines held 52% against 53% on shuffled candles, and EURUSD 44% against 42%.
- `/signals` adds `level_touch` when the last closed candle of the lowest interval interacted with a top zone, with
  the historical hold share for that kind of zone. `/session-story` shows the nearest zones under "Lines & reactions".

## Top-down read (`/mtf`)

- **Box.** On each timeframe the box comes from confirmed body swings: top = the higher of the last two swing-high
  bodies, bottom = the lower of the last two swing-low bodies, while the swings are not trending. A swing that is
  the origin of an impulse leg is left out, so a box starts where the impulse ended. While closes stay inside, the
  box is redrawn as new swings confirm; the first box of the consolidation is kept as `first_box`.
- **Break, sweep, fakeout.** A body close must clear an edge by 0.1 ATR to count as a break. A wick beyond an edge
  with the close back inside is a sweep. A body close outside that is back inside within `fakeout_max` candles
  (default 3) is a fakeout. A broken box stays the reference for 24 candles, then it becomes history.
- **Playbook.** The plan uses the context timeframe's box while price is inside it or has just closed outside it
  (within 12 candles and 3 ATR); otherwise the setup timeframe's box. `cases` lists the entry at the edge, the
  no-trade cases, and the flip when a body closes back inside or breaks the edge.
- **Odds.** Edge fades, sweeps, breaks, fakeouts and retests are counted over all available 1h and 4h history, with
  boxes detected only from earlier candles. Each rate is compared with the same rules on shuffled candles; the
  verdict says `no edge` unless the rate differs clearly (|z| ≥ 2.58, n ≥ 30). In live runs on XAUUSD nothing
  differed from shuffled candles.
- **Limits.** One box per timeframe: an inner box inside a wider one is not tracked separately, and once the box
  has been redrawn the old wide edges only survive as `prior_box_edges`. The user's hand-drawn top line can sit at a
  lower-timeframe body or a wick, where this detector uses the timeframe's own bodies.
- **Gold candles.** H4 candles for spot metals start with the 18:00 New York session open (FX uses 17:00). On days
  with cached Dukascopy files the candles are real spot candles; other days are futures minus an interpolated basis.
- `/signals` adds `range_sweep` (wick beyond a box edge that closed back inside) and `range_edge` (first arrival in
  an edge zone).
