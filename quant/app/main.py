"""View Engine quant sidecar — FastAPI app.

Run: cd quant && uv run uvicorn app.main:app --host 127.0.0.1 --port 8090
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .cache import cache
from .calendar import get_calendar
from .correlation import correlation_report
from .data import (DataError, candles, get_price, get_series, iso, norm_interval, now_ts, parse_as_of, replay,
                   replay_as_of)
from .model import ENGINE, predict
from .options import get_options
from .sessions import get_sessions
from .snapshot import analysis, jsonable, snapshot
from .symbols import SymbolError, list_symbols, normalize

VERSION = "0.1.0"
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="View Engine Quant Sidecar", version=VERSION,
              description="Free market data + quant analytics + ML probabilities. Not financial advice.")


def ok(payload: dict) -> JSONResponse:
    """JSON response; in replay mode echoes the replay timestamp (`as_of` stays the data timestamp)."""
    r = replay_as_of()
    if r is not None:
        payload = {**payload, "replay": True, "replay_as_of": iso(r),
                   "replay_note": "computed only from candles that had closed by replay_as_of"}
    return JSONResponse(jsonable(payload))


AS_OF = Query(None, description="Replay: UTC ISO timestamp; only candles closed by then are used")
HIST = Query(None, ge=1, le=1500, description="Deep intraday history in days (Dukascopy, when available)")


@app.exception_handler(SymbolError)
async def _sym_err(_: Request, e: SymbolError):
    return JSONResponse({"error": str(e), "kind": "bad_symbol"}, status_code=400)


@app.exception_handler(ValueError)
async def _val_err(_: Request, e: ValueError):
    return JSONResponse({"error": str(e), "kind": "bad_request"}, status_code=400)


@app.exception_handler(DataError)
async def _data_err(_: Request, e: DataError):
    return JSONResponse({"error": str(e)[:600], "kind": "no_data"}, status_code=404)


@app.exception_handler(Exception)
async def _any_err(_: Request, e: Exception):
    logging.getLogger("quant").exception("unhandled")
    return JSONResponse({"error": f"{type(e).__name__}: {str(e)[:400]}", "kind": "internal"}, status_code=500)


@app.get("/health")
def health():
    from . import dukascopy as dk
    from .data import oanda_enabled
    return {"status": "ok", "version": VERSION, "engine": ENGINE, "oanda_enabled": oanda_enabled(),
            "dukascopy": dk.status(), "cache": cache.stats(), "now_utc": iso(datetime.now(timezone.utc))}


@app.get("/symbols")
def symbols():
    return {"count": len(list_symbols()), "symbols": list_symbols(),
            "note": "Unknown tickers pass through to Yahoo as equities/ETFs; any 6-letter ISO FX pair works."}


@app.get("/ohlc")
def ohlc(symbol: str, interval: str = "1d", lookback: int = Query(200, ge=2, le=5000),
         start: Optional[str] = Query(None, description="UTC ISO: first candle start (inclusive)"),
         end: Optional[str] = Query(None, description="UTC ISO: last candle start (inclusive)"),
         history_days: Optional[int] = HIST, as_of: Optional[str] = AS_OF):
    sym = normalize(symbol)
    interval = norm_interval(interval)
    t0, t1 = parse_as_of(start, True), parse_as_of(end, True)
    with replay(as_of):
        if t0 is not None and history_days is None:
            from .data import default_history_days
            age = (now_ts() - t0).days + 2
            base = get_series(sym, interval)
            if base.df.index[0] > t0 and default_history_days(sym):
                history_days = min(max(age, 30), 1500)  # Yahoo does not reach back that far -> deep history
        s = get_series(sym, interval, history_days)
        df = s.df
        if t0 is not None or t1 is not None:
            if t0 is not None:
                df = df[df.index >= t0]
            if t1 is not None:
                df = df[df.index <= t1]
            df = df.iloc[:5000]
            if df.empty:
                raise DataError(f"no {interval} candles for {sym.id} between {start} and {end} "
                                f"(history available: {iso(s.df.index[0])} → {iso(s.df.index[-1])})")
        else:
            df = df.iloc[-lookback:]
        last, prev = float(df["c"].iloc[-1]), float(df["c"].iloc[-2]) if len(df) > 1 else None
        return ok({"symbol": sym.id, "interval": interval, **s.source_info, "ticker": s.ticker,
                   "as_of": iso(df.index[-1]), "range": {"start": iso(df.index[0]), "end": iso(df.index[-1])},
                   "delayed_minutes": s.delayed_minutes, "last_bar_age_minutes": s.last_bar_age_minutes,
                   "count": len(df), "last": last, "change_pct": (last / prev - 1) * 100 if prev else None,
                   "candles": candles(df)})


@app.get("/price")
def price(symbol: str, as_of: Optional[str] = AS_OF):
    sym = normalize(symbol)
    with replay(as_of):
        return ok({"symbol": sym.id, **get_price(sym)})


@app.get("/analysis")
def analysis_ep(symbol: str, interval: str = "1d", as_of: Optional[str] = AS_OF):
    with replay(as_of):
        return ok(analysis(normalize(symbol), interval))


@app.get("/correlation")
def correlation(symbols: str = "USDJPY,DXY,US10Y", interval: str = "1d", window: int = Query(60, ge=10, le=1000),
                as_of: Optional[str] = AS_OF):
    syms, seen = [], set()
    for raw in symbols.split(","):
        if raw.strip():
            s = normalize(raw)
            if s.id not in seen:
                seen.add(s.id)
                syms.append(s)
    if len(syms) > 12:
        raise ValueError("at most 12 symbols")
    with replay(as_of):
        rep = correlation_report(syms, norm_interval(interval), window)
        base_src = rep["sources"].get(rep["base"], {})
        return ok({**rep, "source": base_src.get("source"), "as_of": base_src.get("as_of")})


@app.get("/calendar")
def calendar(currencies: Optional[str] = "USD", impact: Optional[str] = "High,Medium",
             days: float = Query(7, ge=0, le=14), past_hours: float = Query(24, ge=0, le=168)):
    cur = currencies.split(",") if currencies and currencies.lower() != "all" else None
    imp = impact.split(",") if impact and impact.lower() != "all" else None
    return ok(get_calendar(cur, imp, days, past_hours))


@app.get("/sessions")
def sessions(as_of: Optional[str] = AS_OF):
    with replay(as_of):
        return ok(get_sessions())


@app.get("/options")
def options(symbol: str, expiries: int = Query(3, ge=1, le=8)):
    return ok(get_options(normalize(symbol), expiries))


@app.get("/predict")
def predict_ep(symbol: str, interval: str = "1d", horizon: str = "1", as_of: Optional[str] = AS_OF):
    with replay(as_of):
        return ok(predict(normalize(symbol), interval, horizon))


@app.get("/snapshot")
def snapshot_ep(symbol: str, timeframes: str = "1d,4h,1h", horizon: str = "1d",
                expiries: int = Query(3, ge=1, le=6), as_of: Optional[str] = AS_OF):
    sym = normalize(symbol)
    tfs = [t for t in timeframes.split(",") if t.strip()] or ["1d"]
    for t in tfs:
        norm_interval(t)  # validate early -> 400
    with replay(as_of):
        snap = snapshot(sym, tfs, horizon, expiries)
        if not snap.get("timeframes"):
            raise DataError(f"no price data for {sym.id}: " + "; ".join(
                f"{k}: {v}" for k, v in snap["errors"].items() if k.startswith("tf:"))[:500])
        return ok(snap)


@app.get("/structure")
def structure_ep(symbol: str, interval: str = "1h", lookback: int = Query(300, ge=50, le=5000),
                 left: int = Query(2, ge=1, le=10), right: int = Query(2, ge=1, le=10),
                 as_of: Optional[str] = AS_OF):
    from .structure import analyze as structure_analyze
    with replay(as_of):
        return ok(structure_analyze(normalize(symbol), norm_interval(interval), lookback, left, right))


@app.get("/structure/model")
def structure_model_ep(symbol: str, interval: str = "1h", k: int = Query(24, ge=3, le=200),
                       as_of: Optional[str] = AS_OF):
    from .model import public_structure_model, structure_model
    with replay(as_of):
        return ok(public_structure_model(structure_model(normalize(symbol), norm_interval(interval), k)))


@app.get("/signals")
def signals_ep(symbol: str, intervals: str = "4h,1h", as_of: Optional[str] = AS_OF):
    from .structure import signals
    ivs = [norm_interval(i) for i in intervals.split(",") if i.strip()][:4]
    if not ivs:
        raise ValueError("intervals required")
    with replay(as_of):
        return ok(signals(normalize(symbol), ivs))


@app.get("/backtest")
def backtest_ep(symbol: str, interval: str = "1h", k: int = Query(24, ge=1, le=500),
                target_r: float = Query(1.0, gt=0, le=20), cost_r: float = Query(0.05, ge=0, le=1),
                history_days: Optional[int] = HIST, as_of: Optional[str] = AS_OF):
    from .backtest import backtest
    with replay(as_of):
        return ok(backtest(normalize(symbol), norm_interval(interval), k, target_r, cost_r, history_days))


@app.get("/backtest/matrix")
def backtest_matrix_ep(symbols: str = "USDJPY,EURUSD,XAUUSD", intervals: str = "4h,1h",
                       k: int = Query(24, ge=1, le=500), target_r: float = Query(1.0, gt=0, le=20),
                       cost_r: float = Query(0.05, ge=0, le=1), history_days: Optional[int] = HIST,
                       as_of: Optional[str] = AS_OF):
    from .backtest import backtest_matrix
    syms = list({s.id: s for s in (normalize(x) for x in symbols.split(",") if x.strip())}.values())
    ivs = list(dict.fromkeys(norm_interval(i) for i in intervals.split(",") if i.strip()))
    if not syms or not ivs:
        raise ValueError("symbols and intervals are required")
    if len(syms) * len(ivs) > 24:
        raise ValueError("at most 24 symbol x interval cells")
    with replay(as_of):
        return ok(backtest_matrix(syms, ivs, k, target_r, cost_r, history_days))


@app.get("/retest")
def retest_ep(symbol: str, interval: str = "15m", level_interval: str = "1h",
              pip: Optional[float] = Query(None, gt=0), sl_pips: float = Query(25, gt=0),
              tp_pips: float = Query(75, gt=0), max_wait: int = Query(48, ge=1, le=1000),
              k: int = Query(64, ge=1, le=2000), tol_pips: float = Query(3, ge=0),
              spread_pips: Optional[float] = Query(None, ge=0), levels: str = "both",
              grid_entry: str = "reject_close", history_days: Optional[int] = HIST,
              as_of: Optional[str] = AS_OF):
    from .retest import retest
    with replay(as_of):
        return ok(retest(normalize(symbol), norm_interval(interval), norm_interval(level_interval), pip, sl_pips,
                         tp_pips, max_wait, k, tol_pips, spread_pips, levels, grid_entry, history_days))


@app.get("/session-story")
def session_story_ep(symbol: str, interval: str = "15m", days: int = Query(3, ge=1, le=10),
                     pip: Optional[float] = Query(None, gt=0), sl_pips: float = Query(25, gt=0),
                     tp_pips: float = Query(75, gt=0), level_interval: str = "1h",
                     as_of: Optional[str] = AS_OF):
    from .story import session_story
    with replay(as_of):
        return ok(session_story(normalize(symbol), norm_interval(interval), days, pip, sl_pips, tp_pips,
                                norm_interval(level_interval)))


@app.get("/levels")
def levels_ep(symbol: str, intervals: str = "4h,1h,15m", pip: Optional[float] = Query(None, gt=0),
              history_days: Optional[int] = HIST, as_of: Optional[str] = AS_OF):
    from .levels import levels
    ivs = list(dict.fromkeys(norm_interval(i) for i in intervals.split(",") if i.strip()))
    if not ivs:
        raise ValueError("intervals required")
    with replay(as_of):
        return ok(levels(normalize(symbol), ivs, pip, history_days))


@app.get("/mtf")
def mtf_ep(symbol: str, intervals: str = "4h,1h,15m,5m", pip: Optional[float] = Query(None, gt=0),
           sl_pips: float = Query(25, gt=0), tp_pips: float = Query(50, gt=0), tp2_pips: float = Query(100, gt=0),
           fakeout_max: int = Query(3, ge=1, le=12), as_of: Optional[str] = AS_OF):
    from .mtf import mtf
    ivs = list(dict.fromkeys(norm_interval(i) for i in intervals.split(",") if i.strip()))
    if not ivs:
        raise ValueError("intervals required")
    with replay(as_of):
        return ok(mtf(normalize(symbol), ivs, pip, sl_pips, tp_pips, tp2_pips, fakeout_max))


@app.get("/chart")
def chart_ep(symbol: str, interval: str = "15m", bars: int = Query(300, ge=50, le=1000),
             pip: Optional[float] = Query(None, gt=0), sl_pips: float = Query(20, gt=0),
             tp_pips: float = Query(50, gt=0), tp2_pips: float = Query(100, gt=0), as_of: Optional[str] = AS_OF):
    """Candles of one interval plus every drawing (swings, boxes, zones, events, impulses, patterns, sessions,
    news), the playbook and a plain reading with a stated risk rule. Cached per closed candle."""
    from .chart import chart
    with replay(as_of):
        return ok(chart(normalize(symbol), norm_interval(interval), bars, pip, sl_pips, tp_pips, tp2_pips))


def _trigger_interval(interval: str) -> str:
    """Trigger timeframe of /dataset and /features: here '1m' means one minute."""
    iv = (interval or "").strip().lower()
    return "1min" if iv in ("1m", "1min") else norm_interval(interval)


@app.get("/events/history")
def events_history(currency: Optional[str] = None, start: Optional[str] = Query(None, alias="from"),
                   end: Optional[str] = Query(None, alias="to")):
    """Scheduled high-impact events (official schedules, 2019 -> what is already scheduled)."""
    from . import events as ev
    rows = ev.history(currency, start, end)
    t = ev.load_table()
    return ok({"count": len(rows), "events": rows, "coverage": ev.coverage(), "report": t.get("report"),
               "source": "FRED / ALFRED release dates, federalreserve.gov, ecb.europa.eu, boj.or.jp",
               "as_of": t.get("built")})


@app.get("/dataset")
def dataset_ep(symbol: str, interval: str = "15m", pip: Optional[float] = Query(None, gt=0),
               sl_pips: float = Query(20, gt=0), tp_pips: float = Query(50, gt=0), tp2_pips: float = Query(100, gt=0),
               horizon: int = Query(48, ge=1, le=2000), spread_pips: Optional[float] = Query(None, ge=0),
               max_rows: int = Query(20000, ge=1, le=200000), start: Optional[str] = None, end: Optional[str] = None,
               format: str = "json", as_of: Optional[str] = AS_OF):
    """Feature matrix + outcome labels, one row per closed candle. format=json (capped by max_rows) or
    format=npz (binary, no row cap, cached on disk)."""
    from fastapi.responses import FileResponse

    from .dataset import dataset, dataset_npz
    if format not in ("json", "npz"):
        raise ValueError("format must be json or npz")
    iv = _trigger_interval(interval)
    with replay(as_of):
        if format == "npz":
            path = dataset_npz(normalize(symbol), iv, pip, sl_pips, tp_pips, tp2_pips, horizon, spread_pips, start,
                               end)
            return FileResponse(path, media_type="application/octet-stream", filename=path.name)
        out = dataset(normalize(symbol), iv, pip, sl_pips, tp_pips, tp2_pips, horizon, spread_pips, max_rows, start,
                      end)
        r = replay_as_of()
        if r is not None:
            out = {**out, "replay": True, "replay_as_of": iso(r)}
        return JSONResponse(out)


@app.get("/features")
def features_ep(symbol: str, interval: str = "15m", pip: Optional[float] = Query(None, gt=0),
                sl_pips: float = Query(20, gt=0), tp_pips: float = Query(50, gt=0),
                tp2_pips: float = Query(100, gt=0), horizon: int = Query(48, ge=1, le=2000),
                spread_pips: Optional[float] = Query(None, ge=0), as_of: Optional[str] = AS_OF):
    from .dataset import features
    with replay(as_of):
        out = features(normalize(symbol), _trigger_interval(interval), pip, sl_pips, tp_pips, tp2_pips, horizon,
                       spread_pips)
        x = out.pop("x")  # already cleaned exactly like the /dataset rows; keep it bit-identical
        r = replay_as_of()
        payload = jsonable({**out, **({"replay": True, "replay_as_of": iso(r)} if r is not None else {})})
        payload["x"] = x
        return JSONResponse(payload)


@app.post("/history/import")
def history_import(body: dict):
    """Start a background import into the local M1 store. Body: {symbols: [...], from_year: 2021,
    source: "histdata" | "dukascopy" | "folder", tz: "UTC"} (tz = default for drop-folder files)."""
    from . import m1store
    source = str(body.get("source", "histdata")).lower()
    if source not in ("histdata", "dukascopy", "folder"):
        raise ValueError("source must be histdata, dukascopy or folder")
    syms = [normalize(x).id for x in (body.get("symbols") or [])]
    if source != "folder" and not syms:
        raise ValueError("symbols required")
    job = m1store.start_import(syms, int(body.get("from_year", 2021)), source, str(body.get("tz", "UTC")))
    return ok({"job": job, "status": "running", "source": source, "symbols": syms,
               "as_of": iso(datetime.now(timezone.utc))})


@app.get("/history/import/{job}")
def history_import_job(job: str):
    from . import m1store
    j = m1store.jobs.get(job)
    if j is None:
        raise DataError(f"unknown import job {job}")
    return ok({**j, "source": j["source"], "as_of": iso(datetime.now(timezone.utc))})


@app.get("/history/coverage")
def history_coverage(symbol: Optional[str] = None):
    """Local M1 store coverage: per symbol the sources, first/last minute, rows and gaps longer than a weekend."""
    from . import m1store
    cov = m1store.coverage(normalize(symbol).id if symbol else None)
    return ok({"store": str(m1store.STORE_DIR), "symbols": cov, "count": len(cov),
               "source": "local M1 store", "as_of": iso(datetime.now(timezone.utc))})


@app.get("/history/status")
def history_status(symbol: str):
    """Dukascopy disk-cache coverage for a symbol (deep intraday history)."""
    from . import dukascopy as dk
    sym = normalize(symbol)
    inst = dk.instrument(sym)
    if not inst:
        raise DataError(f"{sym.id}: no Dukascopy instrument (FX majors/JPY crosses, XAUUSD, XAGUSD only)")
    return ok({"symbol": sym.id, **dk.cache_status(inst), "source": "dukascopy public datafeed",
               "as_of": iso(datetime.now(timezone.utc))})
