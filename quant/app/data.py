"""Market data access: Yahoo (yfinance, then raw chart API), FRED, stooq fallbacks.

All frames returned have a UTC DatetimeIndex and float columns o,h,l,c,v.
Daily/weekly/monthly bars are stamped at 00:00 UTC of their (exchange-local) date.
"""
from __future__ import annotations

import contextlib
import contextvars
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
    history: Optional[dict] = None  # set when deep history (Dukascopy) was requested / used

    @property
    def source_info(self) -> dict:
        """Provenance fields to merge into any response built from this series."""
        out = {"source": self.source, "basis_adjusted": bool(self.basis)}
        if self.basis:
            pub = dict(self.basis)
            if pub.get("curve"):
                pub["curve"] = {k: v for k, v in pub["curve"].items() if k not in ("days", "basis")}
            out.update(basis=self.basis["basis"], basis_info=pub)
        if self.history:
            out["history"] = self.history
        return out

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


# ---------------------------------------------------------------- clocks / replay mode

_REPLAY: contextvars.ContextVar = contextvars.ContextVar("replay_as_of", default=None)


def wall_utc() -> datetime:
    """Real wall-clock time (cache/backoff bookkeeping)."""
    return datetime.now(timezone.utc)


def replay_as_of() -> Optional[pd.Timestamp]:
    return _REPLAY.get()


def now_ts() -> pd.Timestamp:
    """'Now' for all analytics: the replay timestamp when one is set, else the wall clock."""
    return _REPLAY.get() or pd.Timestamp.now(tz="UTC")


def now_utc() -> datetime:
    return now_ts().to_pydatetime()


def parse_as_of(value: Optional[str], allow_future: bool = False) -> Optional[pd.Timestamp]:
    if value is None or not str(value).strip():
        return None
    try:
        ts = pd.Timestamp(str(value).strip())
    except Exception:
        raise ValueError(f"bad timestamp '{value}' (use UTC ISO, e.g. 2026-09-12T14:30:00Z)")
    if pd.isna(ts):
        raise ValueError(f"bad timestamp '{value}'")
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    if not allow_future and ts > pd.Timestamp.now(tz="UTC"):
        raise ValueError("as_of is in the future")
    return ts


@contextlib.contextmanager
def replay(as_of):
    """Run a block as if the current time were `as_of` (None = live)."""
    ts = parse_as_of(as_of) if isinstance(as_of, str) or as_of is None else as_of
    tok = _REPLAY.set(ts)
    try:
        yield ts
    finally:
        _REPLAY.reset(tok)


def submit_ctx(ex, fn, *args):
    """ThreadPoolExecutor.submit that carries the replay context into the worker thread."""
    return ex.submit(contextvars.copy_context().run, fn, *args)


def replay_key():
    r = _REPLAY.get()
    return iso(r) if r is not None else None


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
    df.index = idx.as_unit("ns")
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


def h4_offset(sym: Optional[Symbol]) -> int:
    """Hour offset of the H4 grid in New York local time: FX bars start 17:00 NY (17/21/01/05/09/13), spot
    metals start with their 18:00 NY session open (18/22/02/06/10/14), as on TradingView / MT brokers."""
    return 2 if sym is not None and sym.asset_class == "metal" else 1


def resample_ohlc(df: pd.DataFrame, rule: str, h4_off: int = 1) -> pd.DataFrame:
    agg = {"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"}
    if rule == "4h":
        # H4 bins on a New York-local grid (DST-aware): offset 1 -> 01/05/09/13/17/21, offset 2 -> 02/06/.../22
        ny = df.index.tz_convert("America/New_York")
        local = ny.tz_localize(None) - pd.Timedelta(hours=h4_off)
        start_local = local.floor("4h") + pd.Timedelta(hours=h4_off)
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
    df.index = idx.as_unit("ns")
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
    out.index = pd.DatetimeIndex(out.index).tz_localize("UTC").as_unit("ns")
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
                df = resample_ohlc(df, rule, h4_offset(sym))
            if len(df) < 2:
                raise DataError("fewer than 2 bars")
            return Series(df=df, source=source, ticker=ticker, interval=interval,
                          delayed_minutes=estimate_delay_minutes(ticker, source))
        except Exception as e:  # try next source
            errors.append(f"{source}:{ticker}: {e}")
            log.info("data source failed %s", errors[-1])
    raise DataError(f"no data for {sym.id} @ {interval}: " + " | ".join(errors))


def day_close_mode(sym: Symbol):
    """Daily-bar close convention: 'fx' (17:00 NY), 'us' (16:00 NY cash close) or False (00:00 UTC)."""
    t = sym.yahoo or ""
    if sym.asset_class in ("fx", "metal", "commodity") or t.endswith(("=X", ".NYB", "=F")):
        return "fx"
    if sym.asset_class in ("etf", "equity") and "." not in t or t in ("^GSPC", "^NDX", "^DJI", "^VIX", "^TNX"):
        return "us"
    return False


def ns_index(values) -> pd.DatetimeIndex:
    """UTC DatetimeIndex with nanosecond unit (pandas 3 infers s/us units; merges need one unit)."""
    idx = pd.DatetimeIndex(values)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    return idx.as_unit("ns")


def close_times(index: pd.DatetimeIndex, interval: str, mode=False) -> pd.DatetimeIndex:
    if INTERVALS[interval][3]:
        return index + pd.Timedelta(hours=INTERVALS[interval][4])
    return ns_index([bar_close_time(t, interval, mode) for t in index])


def truncate_to(series: Series, sym: Symbol, as_of: pd.Timestamp) -> Series:
    """Keep only candles that had CLOSED by `as_of` (replay mode)."""
    df = series.df
    keep = close_times(df.index, series.interval, day_close_mode(sym)) <= as_of
    df = df[keep]
    if len(df) < 2:
        raise DataError(f"{sym.id} @ {series.interval}: no candles closed by {iso(as_of)} in the available history "
                        f"(source {series.source} starts {iso(series.df.index[0]) if len(series.df) else 'n/a'})")
    return Series(df=df, source=series.source, ticker=series.ticker, interval=series.interval,
                  delayed_minutes=series.delayed_minutes, fetched_at=series.fetched_at, basis=series.basis,
                  history=series.history)


def _live_series(sym: Symbol, interval: str) -> Series:
    ttl = TTL_INTRADAY if INTERVALS[interval][3] else TTL_DAILY
    raw = cache.get_or_set(("ohlc", sym.id, sym.yahoo, interval), ttl, lambda: _raw_series(sym, interval))
    if sym.spot_code and raw.source != "oanda":
        b = get_basis(sym)
        if b and b.get("basis") is not None:
            b = with_basis_curve(sym, b)
            sig = tuple(b["curve"]["anchors"]) if b.get("curve") else ()
            days = spot_days(sym)
            return cache.get_or_set(("ohlc_spot", sym.id, interval, raw.fetched_at, b["basis"], sig, days), ttl,
                                    lambda: spot_overlay(sym, apply_basis(raw, b), days))
    return raw


def spot_days(sym: Symbol) -> tuple:
    """Dates (YYYY-MM-DD) for which real spot M1 candles are cached on disk (Dukascopy)."""
    from . import dukascopy as dk
    inst = dk.instrument(sym)
    folder = dk.CACHE_DIR / inst if inst else None
    if not inst or not folder.exists():
        return ()
    return tuple(sorted(f.stem for f in folder.glob("*.bi5") if f.stat().st_size > 0))


_spot_m1_cache: dict[tuple, pd.DataFrame] = {}


def _spot_m1(sym: Symbol, days: tuple) -> pd.DataFrame:
    from . import dukascopy as dk
    inst = dk.instrument(sym)
    key = (inst, days)
    if key not in _spot_m1_cache:
        frames = []
        for d in days:
            try:
                dd = datetime.strptime(d, "%Y-%m-%d").date()
                frames.append(dk.decode_bi5((dk.CACHE_DIR / inst / f"{d}.bi5").read_bytes(), dd,
                                            dk.point_divisor(inst)))
            except Exception as e:
                log.info("spot overlay: bad file %s: %s", d, e)
        _spot_m1_cache.clear()
        _spot_m1_cache[key] = pd.concat(frames).sort_index() if frames else pd.DataFrame()
    return _spot_m1_cache[key]


def spot_overlay(sym: Symbol, series: Series, days: tuple) -> Series:
    """Replace basis-adjusted futures candles by REAL spot candles on every day that is cached from Dukascopy
    (a candle is replaced only when all UTC dates it spans are cached)."""
    if not days or series.interval not in DEEP_INTERVALS:
        return series
    m1 = _spot_m1(sym, days)
    if m1.empty:
        return series
    deep = resample_m1(m1, series.interval, h4_offset(sym))
    have = set(days)
    if series.interval == "1d":
        start, end = deep.index - pd.Timedelta(days=1), deep.index
    else:
        start = deep.index
        end = deep.index + pd.Timedelta(hours=INTERVALS[series.interval][4]) - pd.Timedelta(minutes=1)
    ok = np.array([a.strftime("%Y-%m-%d") in have and (b.strftime("%Y-%m-%d") in have or b.dayofweek == 5)
                   for a, b in zip(start, end)])
    deep = deep[ok]
    common = deep.index.intersection(series.df.index)
    if not len(common):
        return series
    df = series.df.copy()
    df.loc[common, ["o", "h", "l", "c"]] = deep.loc[common, ["o", "h", "l", "c"]].to_numpy()
    info = {**(series.basis or {}), "spot_overlay": {"provider": "dukascopy M1 bid", "days": len(days),
                                                      "first": days[0], "last": days[-1],
                                                      "candles_replaced": int(len(common))}}
    return Series(df=df, source=series.source + f" + real spot candles (Dukascopy bid) on {len(days)} cached day(s)",
                  ticker=series.ticker, interval=series.interval, delayed_minutes=series.delayed_minutes,
                  fetched_at=series.fetched_at, basis=info, history=series.history)


DEEP_INTERVALS = ("5m", "15m", "30m", "1h", "4h", "1d")
DEFAULT_HISTORY_DAYS = 365


def default_history_days(sym: Symbol) -> Optional[int]:
    from . import dukascopy as dk
    return DEFAULT_HISTORY_DAYS if dk.instrument(sym) and not (oanda_enabled() and oanda_instrument(sym)) else None


def stitch(deep: pd.DataFrame, recent: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Deep history + the newer candles of `recent`, level-shifted by the median close difference on the
    overlap so the two sources line up (e.g. spot history + basis-adjusted futures tail)."""
    info = {"overlap_candles": 0, "tail_offset": 0.0, "tail_candles": 0}
    if deep.empty:
        return recent, info
    common = deep.index.intersection(recent.index)[-200:]
    tail = recent[recent.index > deep.index[-1]]
    if len(common) >= 5:
        off = float((recent.loc[common, "c"] - deep.loc[common, "c"]).median())
        tail = tail.copy()
        tail[["o", "h", "l", "c"]] = tail[["o", "h", "l", "c"]] - off
        info.update(overlap_candles=int(len(common)), tail_offset=off)
    info["tail_candles"] = int(len(tail))
    return pd.concat([deep, tail]), info


def resample_m1(m1: pd.DataFrame, interval: str, h4_off: int = 1) -> pd.DataFrame:
    rule = {"5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h"}.get(interval)
    if rule:
        return resample_ohlc(m1, rule)
    h1 = resample_ohlc(m1, "1h")
    if interval == "4h":
        return resample_ohlc(h1, "4h", h4_off)
    if interval == "1d":
        return fx_daily_from_hourly(h1)
    raise DataError(f"deep history not available for interval {interval}")


def _deep_series(sym: Symbol, interval: str, history_days: int, base: Optional[Series]) -> Optional[Series]:
    """Dukascopy M1 history resampled to `interval`, stitched with the live series for the newest candles."""
    from . import dukascopy as dk
    inst = dk.instrument(sym)
    if not inst or interval not in DEEP_INTERVALS:
        return None
    today = pd.Timestamp.now(tz="UTC").date()
    as_of = replay_as_of()
    end = min(as_of.date(), today - pd.Timedelta(days=1)) if as_of is not None else today - pd.Timedelta(days=1)
    m1, cov = dk.load_m1(inst, end, history_days)
    if not cov["complete"]:
        cov["backfill_running"] = dk.start_backfill(inst, end, history_days)
    hist = {"requested_days": history_days, "provider": "dukascopy M1 BID candles (spot)", **cov}
    if m1.empty:
        if base is None:
            return None
        return Series(df=base.df, source=base.source, ticker=base.ticker, interval=interval,
                      delayed_minutes=base.delayed_minutes, fetched_at=base.fetched_at, basis=base.basis,
                      history={**hist, "used": False})
    key = ("duka", inst, interval, cov["from"], cov["to"], cov["days_used"])
    deep = cache.get_or_set(key, TTL_DAILY, lambda: resample_m1(m1, interval, h4_offset(sym)))
    if base is not None and len(deep) and deep.index[0] >= base.df.index[0]:
        # the cached block does not reach further back than the live source: no gain, keep the live series
        return Series(df=base.df, source=base.source, ticker=base.ticker, interval=interval,
                      delayed_minutes=base.delayed_minutes, fetched_at=base.fetched_at, basis=base.basis,
                      history={**hist, "used": False,
                               "reason": "cached Dukascopy days do not yet extend beyond the live source's history"})
    if base is not None:
        df, st = stitch(deep, base.df)
        src = (f"dukascopy {inst} M1 bid (spot) {cov['from']}→{cov['to']} resampled to {interval}; newer candles: "
               f"{base.source.split(' (')[0]} shifted by {-st['tail_offset']:+.5g} to match")
        return Series(df=df, source=src, ticker=inst, interval=interval, delayed_minutes=base.delayed_minutes,
                      fetched_at=base.fetched_at, basis=None, history={**hist, "used": True, "stitch": st})
    return Series(df=deep, source=f"dukascopy {inst} M1 bid (spot) {cov['from']}→{cov['to']} resampled to {interval}",
                  ticker=inst, interval=interval, delayed_minutes=0, history={**hist, "used": True})


def get_series(sym: Symbol, interval: str, history_days: Optional[int] = None) -> Series:
    """Closed+forming candles for a symbol. `history_days` asks for deep (Dukascopy) intraday history.
    In replay mode the series is cut to candles closed by the replay timestamp."""
    interval = norm_interval(interval)
    as_of = replay_as_of()
    base, err = None, None
    try:
        base = _live_series(sym, interval)
    except DataError as e:
        err = e
    out = base
    need_deep = history_days is not None
    if as_of is not None and base is not None and INTERVALS[interval][3] and not need_deep:
        # replaying a moment the live source barely (or does not) cover -> try deep history
        if as_of - pd.Timedelta(days=10) < base.df.index[0]:
            need_deep, history_days = True, 60
    if need_deep and not (base is not None and base.source == "oanda"):
        try:
            deep = _deep_series(sym, interval, int(history_days), base)
            if deep is not None:
                out = deep
        except Exception as e:
            log.warning("deep history failed for %s %s: %s", sym.id, interval, e)
    if out is None:
        raise err or DataError(f"no data for {sym.id} @ {interval}")
    if as_of is not None:
        if sym.spot_code:  # a real-spot day file for the replay date sharpens the basis there (1 polite request)
            try:
                from . import dukascopy as dk
                if dk.instrument(sym):
                    dk.prefetch_days(dk.instrument(sym), [as_of.date()])
            except Exception:
                pass
        out = truncate_to(out, sym, as_of)
    return out


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
            "quote_time_gap_minutes": gap, "computed_at": iso(wall_utc()), "caveat": BASIS_CAVEAT}


def get_basis(sym: Symbol) -> Optional[dict]:
    """Futures - spot, cached 5 min; falls back to the last good value (<24h, flagged stale)."""
    def load():
        try:
            b = compute_basis(_futures_last(sym), get_spot_metal(sym.spot_code))
            _last_good_basis[sym.id] = {**b, "_t": wall_utc()}
            return b
        except Exception as e:
            lg = _last_good_basis.get(sym.id)
            if lg and (wall_utc() - lg["_t"]).total_seconds() < BASIS_STALE_MAX:
                return {**{k: v for k, v in lg.items() if k != "_t"}, "stale": True, "error": str(e)[:200]}
            log.warning("basis unavailable for %s: %s", sym.id, e)
            return {"basis": None, "error": str(e)[:200]}
    return cache.get_or_set(("basis", sym.id), TTL_BASIS, load)


_anchor_cache: dict[tuple, Optional[float]] = {}
CURVE_CAVEAT = ("Basis is time-varying: measured on days where real spot candles (Dukascopy) are cached, linearly "
                "interpolated between them, carry-extrapolated up to 30 days before the first anchor (flat beyond) "
                "and after the last one. Contract rolls between anchors are not seen. Days with cached spot candles "
                "use those candles directly.")


def basis_anchors(sym: Symbol) -> list[tuple[pd.Timestamp, float]]:
    """(time, futures - spot) for every UTC day with cached Dukascopy candles: median hourly close difference."""
    from . import dukascopy as dk
    inst = dk.instrument(sym)
    folder = dk.CACHE_DIR / inst if inst else None
    if not inst or not folder.exists():
        return []
    fut = None
    out = []
    for f in sorted(folder.glob("*.bi5")):
        key = (inst, f.stem)
        if key not in _anchor_cache:
            val = None
            try:
                d = datetime.strptime(f.stem, "%Y-%m-%d").date()
                m1 = dk.decode_bi5(f.read_bytes(), d, dk.point_divisor(inst))
                if len(m1) >= 240:
                    if fut is None:
                        fut = cache.get_or_set(("ohlc", sym.id, sym.yahoo, "1h"), TTL_INTRADAY,
                                               lambda: _raw_series(sym, "1h")).df["c"]
                    j = pd.concat([fut, resample_ohlc(m1, "1h")["c"]], axis=1, keys=["f", "s"]).dropna()
                    if len(j) >= 6:
                        val = float((j["f"] - j["s"]).median())
            except Exception as e:
                log.info("basis anchor %s failed: %s", f.name, e)
                continue  # do not cache transient failures
            _anchor_cache[key] = val
        if _anchor_cache.get(key) is not None:
            out.append((pd.Timestamp(f.stem, tz="UTC") + pd.Timedelta(hours=12), _anchor_cache[key]))
    return out


def build_basis_curve(anchors: list[tuple[pd.Timestamp, float]], now: pd.Timestamp, live_basis: float) -> Optional[dict]:
    """Piecewise-linear basis(t) through the anchors (level-shifted to hit the live basis now)."""
    anchors = sorted(a for a in anchors if a[0] < now)
    if len(anchors) < 2:
        return None
    t = np.array([(a[0] - now).total_seconds() / 86400 for a in anchors])  # days (negative)
    v = np.array([a[1] for a in anchors])
    slope = float(np.polyfit(t, v, 1)[0]) if (t.max() - t.min()) >= 3 else 0.0  # basis change per day
    # after the last anchor: carry it forward with the measured drift while it is fresh (<= 7 days). The single
    # live quote pair (futures last vs spot) is too noisy to re-level whole days of candles with.
    fresh = abs(t[-1]) <= 7
    end = v[-1] + slope * (0 - t[-1]) if fresh else live_basis
    xs = np.concatenate([[t[0] - 30.0], t, [0.0]])
    ys = np.concatenate([[v[0] - slope * 30.0], v, [end]])
    return {"days": xs.tolist(), "basis": ys.tolist(), "slope_per_day": slope, "basis_now": float(end),
            "live_quote_basis": float(live_basis),
            "anchors": [(a[0].strftime("%Y-%m-%d"), round(a[1], 3)) for a in anchors], "caveat": CURVE_CAVEAT}


def with_basis_curve(sym: Symbol, b: dict) -> dict:
    try:
        now = pd.Timestamp(b.get("futures_as_of") or wall_utc())
        now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
        curve = build_basis_curve(basis_anchors(sym), now, b["basis"])
    except Exception as e:
        log.info("basis curve failed: %s", e)
        curve = None
    return {**b, "curve": curve, "curve_origin": iso(now) if curve else None, "time_varying": curve is not None}


def basis_values(index: pd.DatetimeIndex, b: dict) -> np.ndarray:
    """Basis to subtract at each candle time (constant when no curve is available)."""
    curve = b.get("curve")
    if not curve:
        return np.full(len(index), b["basis"], float)
    origin = pd.Timestamp(b["curve_origin"])
    days = (ns_index(index) - origin).total_seconds().to_numpy() / 86400
    return np.interp(days, curve["days"], curve["basis"])


def apply_basis(raw: Series, b: dict) -> Series:
    df = raw.df.copy()
    shift = basis_values(df.index, b)
    for col in ("o", "h", "l", "c"):
        df[col] = df[col].to_numpy(float) - shift
    return Series(df=df, source=f"{raw.source} {raw.ticker} futures, basis-adjusted to spot ({b['spot_source']})",
                  ticker=raw.ticker, interval=raw.interval, delayed_minutes=raw.delayed_minutes,
                  fetched_at=raw.fetched_at, basis=b)


def get_closes(sym: Symbol, interval: str) -> pd.Series:
    return get_series(sym, interval).df["c"]


def price_as_of(sym: Symbol) -> dict:
    """Replay: last candle close at or before the replay timestamp, from the finest series that covers it."""
    as_of = replay_as_of()
    last_err = None
    for iv in ("5m", "15m", "1h", "1d"):
        try:
            s = get_series(sym, iv)
        except Exception as e:
            last_err = e
            continue
        ct = close_times(s.df.index[-1:], iv, day_close_mode(sym))[0]
        if as_of - ct > pd.Timedelta(hours=INTERVALS[iv][4]) * 3 and iv != "1d" and \
                s.df.index[0] > as_of - pd.Timedelta(days=5):
            continue  # this interval's history does not really reach the replay time
        return {"price": float(s.df["c"].iloc[-1]), "as_of": iso(ct), "price_interval": iv, "ticker": s.ticker,
                "delayed_minutes": 0, **s.source_info}
    raise last_err or DataError(f"no price for {sym.id} at {iso(as_of)}")


def get_price(sym: Symbol) -> dict:
    """Latest price; tries 1-minute Yahoo bars first, falls back to the daily series.
    In replay mode: last candle close <= as_of."""
    if replay_as_of() is not None:
        return price_as_of(sym)

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
