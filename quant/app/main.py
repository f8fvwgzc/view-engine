"""GodView quant sidecar — FastAPI app.

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
from .data import DataError, candles, get_price, get_series, iso, norm_interval
from .model import ENGINE, predict
from .options import get_options
from .sessions import get_sessions
from .snapshot import analysis, jsonable, snapshot
from .symbols import SymbolError, list_symbols, normalize

VERSION = "0.1.0"
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="GodView Quant Sidecar", version=VERSION,
              description="Free market data + quant analytics + ML probabilities. Not financial advice.")


def ok(payload: dict) -> JSONResponse:
    return JSONResponse(jsonable(payload))


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
    from .data import oanda_enabled
    return {"status": "ok", "version": VERSION, "engine": ENGINE, "oanda_enabled": oanda_enabled(), "cache": cache.stats(),
            "now_utc": iso(datetime.now(timezone.utc))}


@app.get("/symbols")
def symbols():
    return {"count": len(list_symbols()), "symbols": list_symbols(),
            "note": "Unknown tickers pass through to Yahoo as equities/ETFs; any 6-letter ISO FX pair works."}


@app.get("/ohlc")
def ohlc(symbol: str, interval: str = "1d", lookback: int = Query(200, ge=2, le=5000)):
    sym = normalize(symbol)
    interval = norm_interval(interval)
    s = get_series(sym, interval)
    df = s.df.iloc[-lookback:]
    last, prev = float(df["c"].iloc[-1]), float(df["c"].iloc[-2]) if len(df) > 1 else None
    return ok({"symbol": sym.id, "interval": interval, **s.source_info, "ticker": s.ticker, "as_of": s.as_of,
               "delayed_minutes": s.delayed_minutes, "last_bar_age_minutes": s.last_bar_age_minutes,
               "count": len(df), "last": last,
               "change_pct": (last / prev - 1) * 100 if prev else None, "candles": candles(df)})


@app.get("/price")
def price(symbol: str):
    sym = normalize(symbol)
    p = get_price(sym)
    return ok({"symbol": sym.id, **p})


@app.get("/analysis")
def analysis_ep(symbol: str, interval: str = "1d"):
    return ok(analysis(normalize(symbol), interval))


@app.get("/correlation")
def correlation(symbols: str = "USDJPY,DXY,US10Y", interval: str = "1d", window: int = Query(60, ge=10, le=1000)):
    syms, seen = [], set()
    for raw in symbols.split(","):
        if raw.strip():
            s = normalize(raw)
            if s.id not in seen:
                seen.add(s.id)
                syms.append(s)
    if len(syms) > 12:
        raise ValueError("at most 12 symbols")
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
def sessions():
    return ok(get_sessions())


@app.get("/options")
def options(symbol: str, expiries: int = Query(3, ge=1, le=8)):
    return ok(get_options(normalize(symbol), expiries))


@app.get("/predict")
def predict_ep(symbol: str, interval: str = "1d", horizon: str = "1"):
    return ok(predict(normalize(symbol), interval, horizon))


@app.get("/snapshot")
def snapshot_ep(symbol: str, timeframes: str = "1d,4h,1h", horizon: str = "1d",
                expiries: int = Query(3, ge=1, le=6)):
    sym = normalize(symbol)
    tfs = [t for t in timeframes.split(",") if t.strip()] or ["1d"]
    for t in tfs:
        norm_interval(t)  # validate early -> 400
    snap = snapshot(sym, tfs, horizon, expiries)
    if not snap.get("timeframes"):
        raise DataError(f"no price data for {sym.id}: " + "; ".join(f"{k}: {v}" for k, v in snap["errors"].items()
                                                                  if k.startswith("tf:"))[:500])
    return ok(snap)


@app.get("/structure")
def structure_ep(symbol: str, interval: str = "1h", lookback: int = Query(300, ge=50, le=5000),
                 left: int = Query(2, ge=1, le=10), right: int = Query(2, ge=1, le=10)):
    from .structure import analyze as structure_analyze
    return ok(structure_analyze(normalize(symbol), norm_interval(interval), lookback, left, right))


@app.get("/structure/model")
def structure_model_ep(symbol: str, interval: str = "1h", k: int = Query(24, ge=3, le=200)):
    from .model import public_structure_model, structure_model
    return ok(public_structure_model(structure_model(normalize(symbol), norm_interval(interval), k)))


@app.get("/signals")
def signals_ep(symbol: str, intervals: str = "4h,1h"):
    from .structure import signals
    ivs = [norm_interval(i) for i in intervals.split(",") if i.strip()][:4]
    if not ivs:
        raise ValueError("intervals required")
    return ok(signals(normalize(symbol), ivs))
