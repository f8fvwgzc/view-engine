# GodView quant sidecar

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
