"""Market data access: Yahoo (yfinance, then raw chart API), FRED, stooq fallbacks.

All frames returned have a UTC DatetimeIndex and float columns o,h,l,c,v.
Daily/weekly/monthly bars are stamped at 00:00 UTC of their (exchange-local) date.
"""
from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx
import numpy as np
import pandas as pd

from .cache import TTL_DAILY, TTL_INTRADAY, cache
from .symbols import Symbol

log = logging.getLogger("quant.data")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class DataError(Exception):
    """No data could be obtained (HTTP 404)."""


# interval -> (yahoo interval, yahoo period, resample rule or None, is_intraday, bar_hours)
INTERVALS: dict[str, tuple[str, str, Optional[str], bool, float]] = {
    "5m": ("5m", "60d", None, True, 5 / 60),
    "15m": ("15m", "60d", None, True, 0.25),
    "30m": ("30m", "60d", None, True, 0.5),
    "1h": ("1h", "730d", None, True, 1.0),
    "4h": ("1h", "730d", "4h", True, 4.0),
    "1d": ("1d", "10y", None, False, 24.0),
    "1wk": ("1wk", "max", None, False, 24.0 * 7),
    "1mo": ("1mo", "max", None, False, 24.0 * 30),
}
INTERVAL_ALIASES = {"1w": "1wk", "w": "1wk", "1wk": "1wk", "d": "1d", "1day": "1d", "daily": "1d", "h": "1h",
                    "60m": "1h", "1hr": "1h", "4hr": "4h", "240m": "4h", "1m": "1mo", "1mon": "1mo", "1mo": "1mo",
                    "m": "1mo", "monthly": "1mo", "weekly": "1wk", "hourly": "1h"}


def norm_interval(iv: str) -> str:
    iv = (iv or "1d").strip().lower()
    iv = INTERVAL_ALIASES.get(iv, iv)
    if iv not in INTERVALS:
        raise ValueError(f"unsupported interval '{iv}'; use one of {list(INTERVALS)}")
    return iv


def bar_hours(interval: str) -> float:
    return INTERVALS[interval][4]


@dataclass
class Series:
    df: pd.DataFrame
    source: str
    ticker: str
    interval: str
    delayed_minutes: int
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    basis: Optional[dict] = None  # set when futures candles were shifted to spot terms

    @property
    def source_info(self) -> dict:
        """Provenance fields to merge into any response built from this series."""
        if not self.basis:
            return {"source": self.source, "basis_adjusted": False}
        return {"source": self.source, "basis_adjusted": True, "basis": self.basis["basis"],
                "basis_info": self.basis}

    @property
    def as_of(self) -> str:
        return iso(self.df.index[-1]) if len(self.df) else iso(self.fetched_at)

    @property
    def last_bar_age_minutes(self) -> Optional[float]:
        if not len(self.df):
            return None
        last = self.df.index[-1]
        if INTERVALS[self.interval][3]:
            # intraday bars are stamped at bar open; age of the bar's open
            return round((now_utc() - last.to_pydatetime()).total_seconds() / 60, 1)
        return None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts) -> str:
    if ts is None:
        return None
    if isinstance(ts, pd.Timestamp):
        ts = ts.to_pydatetime()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def estimate_delay_minutes(ticker: str, source: str) -> int:
    """Rough Yahoo/FRED delay estimates (documented in README)."""
    if source.startswith("fred"):
        return 24 * 60  # end-of-day, ~1 business-day publication lag
    if source.startswith("stooq"):
        return 15
    t = ticker.upper()
    if t.endswith("=X"):
        return 0  # FX quotes ~real-time (indicative, not a dealable feed)
    if t.endswith("=F"):
        return 10  # CME/COMEX/NYMEX/CBOT futures
    if t.endswith(".NYB"):
        return 30  # ICE US (DXY)
    if t in ("^N225",):
        return 20
    if t in ("^TNX", "^FVX", "^IRX", "^TYX", "^VIX"):
        return 15
    if t.startswith("^"):
        return 0  # US cash indices are real-time on Yahoo
    return 0  # US equities/ETFs: real-time (Cboe BZX / Nasdaq basic)


# ---------------------------------------------------------------- normalizers

def _finish(df: pd.DataFrame, intraday: bool) -> pd.DataFrame:
    df = df.copy()
    idx = pd.DatetimeIndex(df.index)
    if intraday:
        idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    else:
        # keep the exchange-local calendar date, stamp it 00:00 UTC
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        idx = idx.normalize().tz_localize("UTC")
    df.index = idx
    df = df[["o", "h", "l", "c", "v"]].astype(float)
    df = df[np.isfinite(df["c"]) & (df["c"] > -1e12)]
    for col in ("o", "h", "l"):
        df[col] = df[col].fillna(df["c"])
    df["v"] = df["v"].fillna(0.0)
    df["h"] = df[["h", "o", "c"]].max(axis=1)
    df["l"] = df[["l", "o", "c"]].min(axis=1)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def drop_fx_closed(df: pd.DataFrame) -> pd.DataFrame:
    """Drop intraday bars that start while spot FX is closed (Fri 17:00 -> Sun 17:00 New York)."""
    ny = df.index.tz_convert("America/New_York")
    wd, hr = ny.dayofweek, ny.hour
    closed = (wd == 5) | ((wd == 4) & (hr >= 17)) | ((wd == 6) & (hr < 17))
    return df[~closed]


def resample_ohlc(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    agg = {"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"}
    if rule == "4h":
        # H4 bins aligned to the 17:00 New York close (01/05/09/13/17/21 NY local, DST-aware), as on OANDA/MT4
        ny = df.index.tz_convert("America/New_York")
        local = ny.tz_localize(None) - pd.Timedelta(hours=1)
        start_local = local.floor("4h") + pd.Timedelta(hours=1)
        key = pd.DatetimeIndex(start_local).tz_localize("America/New_York", ambiguous="NaT",
                                                        nonexistent="shift_forward").tz_convert("UTC")
        g = df[~key.isna()].groupby(key[~key.isna()])
        out = g.agg(agg)
        out.index.name = None
        return out.dropna(subset=["c"])
    out = df.resample(rule, label="left", closed="left", origin="start_day").agg(agg)
    return out.dropna(subset=["c"])


def bar_close_time(ts: pd.Timestamp, interval: str, fx_day=False) -> pd.Timestamp:
    """When the bar that starts at `ts` closes. Daily+ bars: fx_day=True/"fx" -> 17:00 New York on the trade
    date (FX/metals convention); "us" -> 16:00 New York (US cash close); False -> next 00:00 UTC."""
    hrs = INTERVALS[interval][4]
    if interval in ("1d", "1wk", "1mo"):
        if interval == "1mo":
            end = (ts + pd.offsets.MonthBegin(1)).normalize()
        elif interval == "1wk":
            end = ts.normalize() + pd.Timedelta(days=5)  # Friday close
        else:
            end = ts.normalize() + pd.Timedelta(days=1)
        if fx_day:
            d = (end - pd.Timedelta(days=1)).date()
            hour = 16 if fx_day == "us" else 17
            return pd.Timestamp(datetime.combine(d, datetime.min.time()).replace(hour=hour),
                                tz="America/New_York").tz_convert("UTC")
        return end
    return ts + pd.Timedelta(hours=hrs)


# ---------------------------------------------------------------- OANDA v20 (optional, env-gated)

OANDA_GRANULARITY = {"5m": "M5", "15m": "M15", "30m": "M30", "1h": "H1", "4h": "H4", "1d": "D", "1wk": "W",
                     "1mo": "M"}


def oanda_instrument(sym: Symbol) -> Optional[str]:
    """USDJPY -> USD_JPY, XAUUSD -> XAU_USD (FX + metals only)."""
    if sym.asset_class not in ("fx", "metal") or len(sym.id) != 6:
        return None
    return f"{sym.id[:3]}_{sym.id[3:]}"


def _oanda_cfg() -> Optional[tuple[str, dict]]:
    tok = os.environ.get("OANDA_API_TOKEN", "").strip()
    if not tok:
        return None
    env = os.environ.get("OANDA_ENV", "practice").strip().lower()
    base = "https://api-fxtrade.oanda.com" if env == "live" else "https://api-fxpractice.oanda.com"
    return base, {"Authorization": f"Bearer {tok}", "Accept-Datetime-Format": "RFC3339"}


def oanda_enabled() -> bool:
    return _oanda_cfg() is not None


def parse_oanda_candles(payload: dict, interval: str) -> pd.DataFrame:
    rows = []
    for c in payload.get("candles", []):
        m = c.get("mid") or {}
        if not m:
            continue
        rows.append({"t": c["time"], "o": float(m["o"]), "h": float(m["h"]), "l": float(m["l"]),
                     "c": float(m["c"]), "v": float(c.get("volume", 0))})
    if not rows:
        raise DataError("oanda: no candles")
    df = pd.DataFrame(rows)
    idx = pd.DatetimeIndex(pd.to_datetime(df.pop("t"), utc=True))
    if interval in ("1d", "1wk", "1mo"):
        # D/W/M candles start 17:00 New York; stamp with the NY trade date (00:00 UTC), like our FX daily bars
        ny = idx.tz_convert("America/New_York") + pd.Timedelta(hours=7)
        idx = pd.DatetimeIndex(ny.tz_localize(None).normalize()).tz_localize("UTC")
    df.index = idx
    return df[~df.index.duplicated(keep="last")].sort_index()


def _oanda_get(path: str, params: dict) -> dict:
    cfg = _oanda_cfg()
    if cfg is None:
        raise DataError("oanda not configured")
    base, headers = cfg
    r = httpx.get(base + path, params=params, headers=headers, timeout=15)
    if r.status_code != 200:
        # never echo headers/token; body is OANDA's error JSON
        raise DataError(f"oanda HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def _oanda_candles(inst: str, interval: str) -> pd.DataFrame:
    gran = OANDA_GRANULARITY[interval]
    payload = _oanda_get(f"/v3/instruments/{inst}/candles",
                         {"granularity": gran, "count": 5000, "price": "M", "dailyAlignment": 17,
                          "alignmentTimezone": "America/New_York", "weeklyAlignment": "Friday"})
    return parse_oanda_candles(payload, interval)


def _oanda_price(inst: str) -> dict:
    acct = os.environ.get("OANDA_ACCOUNT_ID", "").strip()
    if not acct:
        acct = cache.get_or_set(("oanda_acct",), 24 * 3600,
                                lambda: (_oanda_get("/v3/accounts", {}).get("accounts") or [{}])[0].get("id"))
    if acct:
        try:
            p = _oanda_get(f"/v3/accounts/{acct}/pricing", {"instruments": inst})["prices"][0]
            bid, ask = float(p["bids"][0]["price"]), float(p["asks"][0]["price"])
            return {"price": (bid + ask) / 2, "bid": bid, "ask": ask, "as_of": iso(pd.Timestamp(p["time"])),
                    "tradeable": p.get("tradeable")}
        except Exception as e:
            log.info("oanda pricing failed: %s", e)
    df = parse_oanda_candles(_oanda_get(f"/v3/instruments/{inst}/candles",
                                        {"granularity": "S5", "count": 1, "price": "M"}), "5m")
    return {"price": float(df["c"].iloc[-1]), "as_of": iso(df.index[-1])}


# ---------------------------------------------------------------- fetchers

def _yf_history(ticker: str, yf_interval: str, period: str) -> pd.DataFrame:
    import yfinance as yf

    raw = yf.Ticker(ticker).history(period=period, interval=yf_interval, auto_adjust=False,
                                    actions=False, raise_errors=False)
    if raw is None or raw.empty:
        raise DataError(f"yfinance returned no data for {ticker}")
    raw = raw.rename(columns={"Open": "o", "High": "h", "Low": "l", "Close": "c", "Volume": "v"})
    if "v" not in raw:
        raw["v"] = 0.0
    return raw


def _yahoo_chart(ticker: str, yf_interval: str, period: str) -> pd.DataFrame:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    r = httpx.get(url, params={"interval": yf_interval, "range": period}, headers={"User-Agent": UA},
                  timeout=15, follow_redirects=True)
    if r.status_code != 200:
        raise DataError(f"yahoo chart api HTTP {r.status_code} for {ticker}")
    res = (r.json().get("chart") or {}).get("result") or []
    if not res or not res[0].get("timestamp"):
        raise DataError(f"yahoo chart api: no data for {ticker}")
    res = res[0]
    q = res["indicators"]["quote"][0]
    tzname = res.get("meta", {}).get("exchangeTimezoneName") or "UTC"
    idx = pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert(tzname)
    return pd.DataFrame({"o": q.get("open"), "h": q.get("high"), "l": q.get("low"), "c": q.get("close"),
                         "v": q.get("volume")}, index=idx)


def _fred(series_id: str) -> pd.DataFrame:
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv"
    start = (pd.Timestamp.utcnow() - pd.Timedelta(days=365 * 10)).strftime("%Y-%m-%d")
    # NB: FRED's edge rejects browser-like and unknown custom UAs; the default httpx UA is accepted.
    r = httpx.get(url, params={"id": series_id, "cosd": start}, timeout=20)
    if r.status_code != 200 or not r.text.lower().startswith("observation_date"):
        raise DataError(f"FRED HTTP {r.status_code} for {series_id}")
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = ["date", "c"]
    df["c"] = pd.to_numeric(df["c"], errors="coerce")
    df = df.dropna()
    if df.empty:
        raise DataError(f"FRED: empty series {series_id}")
    df.index = pd.to_datetime(df["date"])
    df = df[["c"]]
    df["o"] = df["h"] = df["l"] = df["c"]
    df["v"] = 0.0
    return df


def _stooq(ticker: str, yf_interval: str) -> pd.DataFrame:
    i = {"1d": "d", "1wk": "w", "1mo": "m"}.get(yf_interval)
    if not i:
        raise DataError("stooq fallback only supports daily/weekly/monthly")
    r = httpx.get("https://stooq.com/q/d/l/", params={"s": ticker, "i": i}, headers={"User-Agent": UA}, timeout=15)
    txt = r.text
    if r.status_code != 200 or not txt.startswith("Date"):
        # stooq currently serves a JS proof-of-work challenge to non-browsers; we do not bypass it.
        raise DataError(f"stooq unavailable for {ticker} (HTTP {r.status_code} / bot challenge)")
    df = pd.read_csv(io.StringIO(txt))
    df = df.rename(columns={"Open": "o", "High": "h", "Low": "l", "Close": "c", "Volume": "v"})
    if "v" not in df:
        df["v"] = 0.0
    df.index = pd.to_datetime(df["Date"])
    return df


# ---------------------------------------------------------------- public API

def fx_daily_from_hourly(hourly: pd.DataFrame) -> pd.DataFrame:
    """FX daily bars with the standard 17:00 New York cut-off, stamped with the NY trade date."""
    ny = hourly.index.tz_convert("America/New_York") + pd.Timedelta(hours=7)
    day = pd.DatetimeIndex(ny.tz_localize(None).normalize())
    g = hourly.groupby(day)
    out = pd.DataFrame({"o": g["o"].first(), "h": g["h"].max(), "l": g["l"].min(), "c": g["c"].last(),
                        "v": g["v"].sum()})
    out.index = pd.DatetimeIndex(out.index).tz_localize("UTC")
    return out[out.index.dayofweek < 5]


def realign_yahoo_fx_daily(daily: pd.DataFrame) -> pd.DataFrame:
    """Yahoo FX daily bar dated D closes at ~D 00:00 London (≈ NY close of D-1) and often has open==close.
    Re-label: close(D) := close of the next Yahoo bar; open := previous close; keep Yahoo's H/L."""
    d = daily[daily.index.dayofweek < 5].copy()
    d["c"] = d["c"].shift(-1)
    d = d.dropna(subset=["c"])
    d["o"] = d["c"].shift(1).fillna(d["o"])
    d["h"] = d[["h", "o", "c"]].max(axis=1)
    d["l"] = d[["l", "o", "c"]].min(axis=1)
    return d


def _fx_daily_series(sym: Symbol, interval: str) -> Series:
    hourly = get_series(sym, "1h").df
    recent = fx_daily_from_hourly(hourly)
    try:
        old = realign_yahoo_fx_daily(_finish(_yf_history(sym.yahoo, "1d", "10y"), False))
        old = old[old.index < recent.index[0]]
        df = pd.concat([old, recent])
    except Exception as e:
        log.info("old FX daily unavailable: %s", e)
        df = recent
    if interval == "1wk":
        df = resample_ohlc(df, "W-MON")
    elif interval == "1mo":
        df = resample_ohlc(df, "MS")
    return Series(df=df, source="yahoo/yfinance 1h -> 17:00 NY-close bars (older history: Yahoo daily, re-aligned)",
                  ticker=sym.yahoo, interval=interval, delayed_minutes=0)


def _raw_series(sym: Symbol, interval: str) -> Series:
    yf_iv, period, rule, intraday, _ = INTERVALS[interval]
    inst = oanda_instrument(sym)
    if inst and oanda_enabled():
        try:
            df = _oanda_candles(inst, interval)
            if len(df) >= 2:
                return Series(df=df, source="oanda", ticker=inst, interval=interval, delayed_minutes=0)
        except Exception as e:
            log.info("oanda candles failed for %s: %s; falling back to Yahoo", inst, e)
    if sym.yahoo and sym.yahoo.endswith("=X") and not intraday:
        try:
            return _fx_daily_series(sym, interval)
        except Exception as e:
            log.info("FX daily build failed (%s); falling back to raw Yahoo daily", e)
    if intraday and not sym.intraday_ok:
        raise DataError(f"{sym.id}: no reliable free intraday source; use interval=1d/1wk/1mo")
    attempts: list[tuple[str, str, callable]] = []
    fred = [("fred", sym.fred, lambda: _fred(sym.fred))] if sym.fred and not intraday else []
    if sym.prefer_fred:
        attempts.extend(fred)
    if sym.yahoo:
        attempts.append(("yahoo/yfinance", sym.yahoo, lambda: _yf_history(sym.yahoo, yf_iv, period)))
        attempts.append(("yahoo/chart-api", sym.yahoo, lambda: _yahoo_chart(sym.yahoo, yf_iv, period)))
    if not sym.prefer_fred:
        attempts.extend(fred)
    if sym.stooq and not intraday:
        attempts.append(("stooq", sym.stooq, lambda: _stooq(sym.stooq, yf_iv)))
    errors = []
    for source, ticker, fn in attempts:
        try:
            df = _finish(fn(), intraday)
            if intraday and ticker.endswith("=X"):
                df = drop_fx_closed(df)
            if source == "fred" and interval in ("1wk", "1mo"):
                df = resample_ohlc(df, "W-MON" if interval == "1wk" else "MS")
            if rule:
                df = resample_ohlc(df, rule)
            if len(df) < 2:
                raise DataError("fewer than 2 bars")
            return Series(df=df, source=source, ticker=ticker, interval=interval,
                          delayed_minutes=estimate_delay_minutes(ticker, source))
        except Exception as e:  # try next source
            errors.append(f"{source}:{ticker}: {e}")
            log.info("data source failed %s", errors[-1])
    raise DataError(f"no data for {sym.id} @ {interval}: " + " | ".join(errors))


def get_series(sym: Symbol, interval: str) -> Series:
    interval = norm_interval(interval)
    ttl = TTL_INTRADAY if INTERVALS[interval][3] else TTL_DAILY
    raw = cache.get_or_set(("ohlc", sym.id, sym.yahoo, interval), ttl, lambda: _raw_series(sym, interval))
    if sym.spot_code and raw.source != "oanda":
        b = get_basis(sym)
        if b and b.get("basis") is not None:
            return cache.get_or_set(("ohlc_spot", sym.id, interval, raw.fetched_at, b["basis"]), ttl,
                                    lambda: apply_basis(raw, b))
    return raw


# ---------------------------------------------------------------- spot metals (futures -> spot basis)

TTL_BASIS = 5 * 60
BASIS_STALE_MAX = 24 * 3600
_last_good_basis: dict[str, dict] = {}
BASIS_CAVEAT = ("Candles are COMEX front-month futures shifted by ONE constant (current futures-minus-spot basis) "
                "so levels match spot charts (e.g. OANDA:XAUUSD). Recent levels are accurate to ~the basis noise; "
                "older history is approximate because the real basis changes with carry/roll.")


def parse_gold_api(js: dict) -> dict:
    return {"price": float(js["price"]), "as_of": js.get("updatedAt"), "source": "gold-api.com spot"}


def parse_swissquote(js: list) -> dict:
    for feed in js:
        for p in feed.get("spreadProfilePrices") or []:
            if p.get("bid") and p.get("ask"):
                ts = feed.get("ts")
                return {"price": (float(p["bid"]) + float(p["ask"])) / 2, "bid": float(p["bid"]),
                        "ask": float(p["ask"]),
                        "as_of": iso(pd.Timestamp(ts, unit="ms", tz="UTC")) if ts else None,
                        "source": "Swissquote public spot quote (mid)"}
    raise DataError("swissquote: no quote")


def _spot_gold_api(code: str) -> dict:
    r = httpx.get(f"https://api.gold-api.com/price/{code}", timeout=10)
    r.raise_for_status()
    return parse_gold_api(r.json())


def _spot_swissquote(code: str) -> dict:
    r = httpx.get(f"https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/{code}/USD",
                  timeout=10)
    r.raise_for_status()
    return parse_swissquote(r.json())


def get_spot_metal(code: str) -> dict:
    """Live spot XAU/XAG in USD from free no-key sources (gold-api.com, then Swissquote)."""
    def load():
        errs = []
        for fn in (_spot_gold_api, _spot_swissquote):
            try:
                return fn(code)
            except Exception as e:
                errs.append(f"{fn.__name__}: {e}")
        raise DataError(f"spot {code} unavailable: " + " | ".join(errs))
    return cache.get_or_set(("spot", code), TTL_INTRADAY, load)


def _futures_last(sym: Symbol) -> dict:
    df = _finish(_yf_history(sym.yahoo, "1m", "5d"), True)
    if not len(df):
        raise DataError("no futures quote")
    return {"price": float(df["c"].iloc[-1]), "as_of": iso(df.index[-1])}


def compute_basis(fut: dict, spot: dict) -> dict:
    gap = None
    try:
        gap = round(abs((pd.Timestamp(fut["as_of"]) - pd.Timestamp(spot["as_of"])).total_seconds()) / 60, 1)
    except Exception:
        pass
    return {"basis": fut["price"] - spot["price"], "futures_price": fut["price"], "futures_as_of": fut["as_of"],
            "spot_price": spot["price"], "spot_as_of": spot.get("as_of"), "spot_source": spot["source"],
            "quote_time_gap_minutes": gap, "computed_at": iso(now_utc()), "caveat": BASIS_CAVEAT}


def get_basis(sym: Symbol) -> Optional[dict]:
    """Futures - spot, cached 5 min; falls back to the last good value (<24h, flagged stale)."""
    def load():
        try:
            b = compute_basis(_futures_last(sym), get_spot_metal(sym.spot_code))
            _last_good_basis[sym.id] = {**b, "_t": now_utc()}
            return b
        except Exception as e:
            lg = _last_good_basis.get(sym.id)
            if lg and (now_utc() - lg["_t"]).total_seconds() < BASIS_STALE_MAX:
                return {**{k: v for k, v in lg.items() if k != "_t"}, "stale": True, "error": str(e)[:200]}
            log.warning("basis unavailable for %s: %s", sym.id, e)
            return {"basis": None, "error": str(e)[:200]}
    return cache.get_or_set(("basis", sym.id), TTL_BASIS, load)


def apply_basis(raw: Series, b: dict) -> Series:
    df = raw.df.copy()
    df[["o", "h", "l", "c"]] = df[["o", "h", "l", "c"]] - b["basis"]
    return Series(df=df, source=f"{raw.source} {raw.ticker} futures, basis-adjusted to spot ({b['spot_source']})",
                  ticker=raw.ticker, interval=raw.interval, delayed_minutes=raw.delayed_minutes,
                  fetched_at=raw.fetched_at, basis=b)


def get_closes(sym: Symbol, interval: str) -> pd.Series:
    return get_series(sym, interval).df["c"]


def get_price(sym: Symbol) -> dict:
    """Latest price; tries 1-minute Yahoo bars first, falls back to the daily series."""
    def _load():
        inst = oanda_instrument(sym)
        if inst and oanda_enabled():
            try:
                return {**_oanda_price(inst), "source": "oanda", "ticker": inst, "delayed_minutes": 0}
            except Exception as e:
                log.info("oanda price failed: %s", e)
        if sym.spot_code:
            try:
                sp = get_spot_metal(sym.spot_code)
                b = get_basis(sym) or {}
                return {**sp, "ticker": f"{sym.spot_code}/USD spot", "delayed_minutes": 0, "spot": True,
                        "basis_vs_futures": b.get("basis"), "futures_ticker": sym.yahoo}
            except Exception as e:
                log.info("spot price failed for %s: %s; using basis-adjusted futures", sym.id, e)
                s1 = get_series(sym, "1h")
                return {"price": float(s1.df["c"].iloc[-1]), "as_of": s1.as_of, "ticker": s1.ticker,
                        "delayed_minutes": s1.delayed_minutes, "spot": False, **s1.source_info}
        if sym.yahoo and sym.intraday_ok and not sym.prefer_fred:
            for fn, src in ((lambda: _yf_history(sym.yahoo, "1m", "5d"), "yahoo/yfinance 1m"),
                            (lambda: _yahoo_chart(sym.yahoo, "1m", "5d"), "yahoo/chart-api 1m")):
                try:
                    df = _finish(fn(), True)
                    if len(df):
                        return {"price": float(df["c"].iloc[-1]), "as_of": iso(df.index[-1]), "source": src,
                                "ticker": sym.yahoo,
                                "delayed_minutes": estimate_delay_minutes(sym.yahoo, "yahoo")}
                except Exception as e:
                    log.info("price %s failed: %s", src, e)
        s = get_series(sym, "1d")
        return {"price": float(s.df["c"].iloc[-1]), "as_of": s.as_of, **s.source_info,
                "source": s.source + " 1d", "ticker": s.ticker, "delayed_minutes": s.delayed_minutes}
    return cache.get_or_set(("price", sym.id, sym.yahoo), TTL_INTRADAY, _load)


def price_for_ticker(ticker: str) -> dict:
    """Latest price for a raw Yahoo ticker (used for option proxies)."""
    tmp = Symbol(id=ticker, name=ticker, asset_class="etf", yahoo=ticker, known=False)
    return get_price(tmp)


def candles(df: pd.DataFrame) -> list[dict]:
    out = []
    for ts, row in zip(df.index, df.itertuples(index=False)):
        out.append({"t": iso(ts), "o": _r(row.o), "h": _r(row.h), "l": _r(row.l), "c": _r(row.c),
                    "v": float(row.v)})
    return out


def _r(x: float) -> Optional[float]:
    if x is None or not np.isfinite(x):
        return None
    return float(f"{x:.6g}") if abs(x) < 1e5 else round(float(x), 2)
