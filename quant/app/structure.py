"""Dow-theory market structure on candle BODIES (house method `dow-structure-body-close`).

Rules implemented (deterministic, closed candles only):
- Swing high/low = fractal pivot of body extremes (max/min of open, close), `left`/`right` bars (default 2).
  Consecutive same-type swings are merged (keep the more extreme) so highs/lows alternate. Labels HH/LH, HL/LL
  (EQH/EQL when equal). A swing is only known `right` bars after it prints (no look-ahead).
- BOS only when a candle CLOSES beyond the reference swing body extreme. A wick beyond with the close back
  inside is a SWEEP (liquidity grab). Each swing can be broken once; it can be swept many times.
- Consolidation box = body of a "mother" candle whose body contains every later close (≥2 inside candles).
  Status: holding / broken (closed beyond) / failed (broken, then closed back inside within 3 candles).
- Impulse candle: |body| ≥ 1.5×ATR14 (ATR of the prior bar) and close in the outer 25% of the candle range.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import indicators as ind
from .data import INTERVALS, bar_close_time, day_close_mode, get_series, iso, now_ts, ns_index
from .sessions import SESSIONS, _occurrences, fx_market_open
from .symbols import Symbol

IMPULSE_BODY_ATR = 1.5
IMPULSE_CLOSE_FRAC = 0.75
BOX_MIN_INSIDE = 2
BOX_MAX_BACK = 30
FAIL_WINDOW = 3
STOP_BUFFER_ATR = 0.2
HTF = {"5m": "1h", "15m": "1h", "30m": "4h", "1h": "4h", "4h": "1d", "1d": "1wk", "1wk": "1mo", "1mo": "1mo"}


# ------------------------------------------------------------------ sessions

@lru_cache(maxsize=20000)
def _session_at(ts_minute: datetime) -> tuple[tuple[str, ...], Optional[str]]:
    active = []
    for name, tzname, ot, ct in SESSIONS:
        tz = ZoneInfo(tzname)
        for o, c in _occurrences(tz, ot, ct, ts_minute, days=range(-1, 2)):
            if o <= ts_minute < c:
                active.append(name)
                break
    # transition windows from the method (DST-aware via London / New York local times)
    ldn = ts_minute.astimezone(ZoneInfo("Europe/London"))
    ny = ts_minute.astimezone(ZoneInfo("America/New_York"))
    trans = None
    if ldn.weekday() < 5 and 8 <= ldn.hour < 9:
        trans = "Tokyo→London"
    elif ny.weekday() < 5 and (ny.hour == 8 or (ny.hour == 9 and ny.minute < 30)):
        trans = "London→New York"
    elif ny.weekday() < 5 and 17 <= ny.hour < 18:
        trans = "New York close"
    return tuple(active), trans


def session_tag(ts) -> dict:
    t = pd.Timestamp(ts).tz_convert("UTC").to_pydatetime().replace(second=0, microsecond=0)
    active, trans = _session_at(t)
    return {"sessions": list(active) or ["off-session"], "transition": trans}


def session_label(ts) -> str:
    st = session_tag(ts)
    s = "+".join(st["sessions"])
    return f"{s} ({st['transition']})" if st["transition"] else s


# ------------------------------------------------------------------ core types

@dataclass
class Swing:
    kind: str  # "H" | "L"
    idx: int
    price: float  # body extreme
    wick: float  # wick extreme of the same candle
    confirmed_idx: int
    label: Optional[str] = None  # HH/LH/EQH/HL/LL/EQL (None for the first of its kind)
    broken: bool = False


@dataclass
class Event:
    type: str  # bos_up | bos_down | sweep_high | sweep_low
    idx: int
    level: float
    close: float
    wick: float
    swing_idx: int
    swing_label: Optional[str]


def bodies(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    o, c = df["o"].to_numpy(float), df["c"].to_numpy(float)
    return np.maximum(o, c), np.minimum(o, c)


def raw_swings(df: pd.DataFrame, left: int = 2, right: int = 2) -> list[Swing]:
    bh, bl = bodies(df)
    hi, lo = df["h"].to_numpy(float), df["l"].to_numpy(float)
    n = len(df)
    if n < left + right + 1:
        return []
    win = np.lib.stride_tricks.sliding_window_view
    i = np.arange(left, n - right)
    prev_max, prev_min = win(bh, left).max(axis=1)[i - left], win(bl, left).min(axis=1)[i - left]
    next_max, next_min = win(bh, right).max(axis=1)[i + 1], win(bl, right).min(axis=1)[i + 1]
    is_h = (bh[i] > prev_max) & (bh[i] >= next_max)
    is_l = (bl[i] < prev_min) & (bl[i] <= next_min)
    out: list[Swing] = []
    for j in np.flatnonzero(is_h | is_l):
        k = int(i[j])
        if is_h[j]:
            out.append(Swing("H", k, float(bh[k]), float(hi[k]), k + right))
        if is_l[j]:
            out.append(Swing("L", k, float(bl[k]), float(lo[k]), k + right))
    return out


def _label(sw: Swing, prev: Optional[Swing]) -> Optional[str]:
    if prev is None:
        return None
    if sw.kind == "H":
        return "HH" if sw.price > prev.price else "LH" if sw.price < prev.price else "EQH"
    return "LL" if sw.price < prev.price else "HL" if sw.price > prev.price else "EQL"


def trend_from_swings(alt: list[Swing]) -> str:
    lh = next((s for s in reversed(alt) if s.kind == "H" and s.label), None)
    ll = next((s for s in reversed(alt) if s.kind == "L" and s.label), None)
    if not lh or not ll:
        return "range"
    if lh.label == "HH" and ll.label == "HL":
        return "up"
    if lh.label == "LH" and ll.label == "LL":
        return "down"
    return "range"


def impulse_flags(df: pd.DataFrame, atr_prev: np.ndarray) -> np.ndarray:
    o, h, l, c = (df[k].to_numpy(float) for k in ("o", "h", "l", "c"))
    rng = np.where(h - l > 0, h - l, np.nan)
    body = np.abs(c - o)
    pos = np.where(c >= o, (c - l) / rng, (h - c) / rng)
    with np.errstate(invalid="ignore"):
        return (body >= IMPULSE_BODY_ATR * atr_prev) & (pos >= IMPULSE_CLOSE_FRAC) & np.isfinite(atr_prev)


@dataclass
class StructureRun:
    swings: list[Swing]  # alternated, labelled, in order
    events: list[Event]
    trend: list[str]  # trend state after each bar (as known at that bar's close)
    last_bos: list[Optional[int]]  # index into events of most recent BOS as of each bar
    history: list[Swing] = field(default_factory=list)  # every swing as it was confirmed in real time
    # (`swings` is the final alternated list: swings later replaced by a more extreme one are dropped from it,
    #  so anything that must be look-ahead free over history should use `history`)


def run_structure(df: pd.DataFrame, left: int = 2, right: int = 2) -> StructureRun:
    """Single causal pass over CLOSED candles."""
    c, h, l = (df[k].to_numpy(float) for k in ("c", "h", "l"))
    by_conf: dict[int, list[Swing]] = {}
    for s in raw_swings(df, left, right):
        by_conf.setdefault(s.confirmed_idx, []).append(s)
    alt: list[Swing] = []
    history: list[Swing] = []
    events: list[Event] = []
    trend, last_bos = [], []
    ref_h: Optional[Swing] = None
    ref_l: Optional[Swing] = None
    bos_i: Optional[int] = None
    for t in range(len(df)):
        if ref_h is not None and not ref_h.broken:
            if c[t] > ref_h.price:
                events.append(Event("bos_up", t, ref_h.price, c[t], h[t], ref_h.idx, ref_h.label))
                ref_h.broken = True
                bos_i = len(events) - 1
            elif h[t] > ref_h.price:
                events.append(Event("sweep_high", t, ref_h.price, c[t], h[t], ref_h.idx, ref_h.label))
        if ref_l is not None and not ref_l.broken:
            if c[t] < ref_l.price:
                events.append(Event("bos_down", t, ref_l.price, c[t], l[t], ref_l.idx, ref_l.label))
                ref_l.broken = True
                bos_i = len(events) - 1
            elif l[t] < ref_l.price:
                events.append(Event("sweep_low", t, ref_l.price, c[t], l[t], ref_l.idx, ref_l.label))
        for s in sorted(by_conf.get(t, []), key=lambda x: x.idx):
            if alt and alt[-1].kind == s.kind:
                better = s.price > alt[-1].price if s.kind == "H" else s.price < alt[-1].price
                if not better:
                    continue
                alt.pop()
            prev = next((x for x in reversed(alt) if x.kind == s.kind), None)
            s.label = _label(s, prev)
            alt.append(s)
            history.append(s)
            if s.kind == "H":
                ref_h = s
            else:
                ref_l = s
        trend.append(trend_from_swings(alt))
        last_bos.append(bos_i)
    return StructureRun(alt, events, trend, last_bos, history)


# ------------------------------------------------------------------ boxes

def box_at(df: pd.DataFrame, end: int, min_inside: int = BOX_MIN_INSIDE, max_back: int = BOX_MAX_BACK,
           _b: Optional[tuple] = None) -> Optional[dict]:
    """Earliest mother candle k (within max_back) whose body contains every close in (k, end]."""
    if end < 1:
        return None
    bh, bl = _b or bodies(df)
    c = df["c"].to_numpy(float)
    best = None
    cmin, cmax = np.inf, -np.inf
    for k in range(end - 1, max(-1, end - max_back - 1), -1):
        cmin, cmax = min(cmin, c[k + 1]), max(cmax, c[k + 1])
        if bl[k] <= cmin and cmax <= bh[k] and end - k >= min_inside:
            best = k
    if best is None:
        return None
    return {"mother_idx": best, "end_idx": end, "low": float(bl[best]), "high": float(bh[best]),
            "n_inside": end - best}


def box_status(df: pd.DataFrame, end: int, _b: Optional[tuple] = None) -> dict:
    """holding / broken / failed / none for the box as of bar `end` (closed)."""
    c = df["c"].to_numpy(float)
    b = box_at(df, end, _b=_b)
    if b:
        return {**b, "status": "holding"}
    for j in range(1, FAIL_WINDOW + 1):
        e0 = end - j
        b0 = box_at(df, e0, _b=_b)
        if not b0:
            continue
        bo = e0 + 1
        if c[bo] > b0["high"]:
            direction = "up"
        elif c[bo] < b0["low"]:
            direction = "down"
        else:  # pragma: no cover - cannot happen (box would still hold)
            continue
        back = [t for t in range(bo + 1, end + 1) if b0["low"] <= c[t] <= b0["high"]]
        status = "failed" if back else "broken"
        return {**b0, "status": status, "direction": direction, "breakout_idx": bo,
                "failed_idx": back[0] if back else None}
    return {"status": "none"}


# ------------------------------------------------------------------ analysis

def next_close_after(last_close: pd.Timestamp, interval: str, now: pd.Timestamp, fx_like: bool) -> pd.Timestamp:
    """Next candle close after `now` (skips the FX weekend for 24x5 instruments)."""
    dur = pd.Timedelta(hours=INTERVALS[interval][4])
    if interval in ("1wk", "1mo"):
        return last_close + (pd.Timedelta(days=7) if interval == "1wk" else pd.Timedelta(days=30))
    t = last_close
    for _ in range(2000):
        t = t + dur
        if interval == "1d" and t.tz_convert("America/New_York").dayofweek >= 5:
            continue
        if t > now and (not fx_like or fx_market_open((t - pd.Timedelta(minutes=1)).to_pydatetime())):
            return t
    return t


def closed_frame(df: pd.DataFrame, interval: str, fx_day: bool, now: Optional[pd.Timestamp] = None):
    now = now or now_ts()
    if INTERVALS[interval][3]:
        closes = df.index + pd.Timedelta(hours=INTERVALS[interval][4])
    else:
        closes = ns_index([bar_close_time(t, interval, fx_day) for t in df.index])
    mask = closes <= now
    forming = None
    if not mask[-1]:
        last = df.iloc[-1]
        forming = {"start": iso(df.index[-1]), "close_time": iso(closes[-1]), "o": float(last["o"]),
                   "h": float(last["h"]), "l": float(last["l"]), "last": float(last["c"])}
    return df[mask], closes[mask], forming, closes


def _swing_dict(s: Swing, df: pd.DataFrame) -> dict:
    t = df.index[s.idx]
    return {"type": s.kind, "label": s.label or ("H" if s.kind == "H" else "L"), "body": s.price, "wick": s.wick,
            "time": iso(t), "session": session_label(t)}


def _event_dict(e: Event, df: pd.DataFrame, closes, atr_prev, imp) -> dict:
    return {"type": e.type, "time": iso(df.index[e.idx]), "close_time": iso(closes[e.idx]),
            "session_at_close": session_label(closes[e.idx]), "level": e.level, "close": e.close, "wick": e.wick,
            "swing_time": iso(df.index[e.swing_idx]), "swing_label": e.swing_label,
            "impulse": bool(imp[e.idx]),
            "body_atr": float(abs(df["c"].iloc[e.idx] - df["o"].iloc[e.idx]) / atr_prev[e.idx])
            if atr_prev[e.idx] and np.isfinite(atr_prev[e.idx]) else None}


def session_levels(hourly: pd.DataFrame, now: pd.Timestamp, n: int = 3) -> dict:
    """Body and wick high/low of the last `n` completed sessions + sessions in progress."""
    done, live = [], []
    nowp = now.to_pydatetime()
    for name, tzname, ot, ct in SESSIONS:
        for o, c in _occurrences(ZoneInfo(tzname), ot, ct, nowp, days=range(-6, 1)):
            sub = hourly[(hourly.index >= o) & (hourly.index < c)]
            if sub.empty:
                continue
            bh, bl = bodies(sub)
            row = {"session": name, "open_utc": iso(o), "close_utc": iso(c), "wick_high": float(sub["h"].max()),
                   "wick_low": float(sub["l"].min()), "body_high": float(bh.max()), "body_low": float(bl.min()),
                   "bars": len(sub)}
            if c <= nowp:
                done.append(row)
            elif o <= nowp:
                live.append({**row, "in_progress": True})
    done.sort(key=lambda r: r["close_utc"], reverse=True)
    return {"previous": done[:n], "in_progress": live}


def is_fx_like(sym: Symbol) -> bool:
    return bool(sym.asset_class in ("fx", "metal") or (sym.yahoo or "").endswith("=X"))


@dataclass
class Context:
    """Closed-candle structure state for a symbol/interval (shared by /signals and the structure model)."""
    sym: Symbol
    interval: str
    source: str
    delayed_minutes: int
    df: pd.DataFrame
    closes: pd.DatetimeIndex
    forming: Optional[dict]
    atr: np.ndarray
    atr_prev: np.ndarray
    imp: np.ndarray
    run: StructureRun
    htf: str
    htf_trend: np.ndarray  # higher-TF trend (+1/0/-1) as known at each bar's close
    trend_num: np.ndarray
    source_info: Optional[dict] = None


_TN = {"up": 1, "down": -1, "range": 0}


def htf_trend_series(sym: Symbol, htf: str, now: pd.Timestamp, max_bars: int = 100_000) -> pd.Series:
    s = get_series(sym, htf)
    hdf, hcl, _, _ = closed_frame(s.df, htf, day_close_mode(sym), now)
    hdf, hcl = hdf.iloc[-max_bars:], hcl[-max_bars:]
    if len(hdf) < 10:
        return pd.Series(dtype=float)
    r = run_structure(hdf)
    return pd.Series([_TN[x] for x in r.trend], index=hcl)


def build_context(sym: Symbol, interval: str, now: Optional[pd.Timestamp] = None,
                  max_bars: int = 6000, history_days: Optional[int] = None, with_htf: bool = True) -> Context:
    """`max_bars` caps the candles analysed (the local M1 store can hold years); `history_days` narrows it to
    that many days and, without a store, asks the Dukascopy cache for deeper history."""
    from .data import bars_for_days
    now = now or now_ts()
    series = get_series(sym, interval, history_days)
    df, closes, forming, _ = closed_frame(series.df, interval, day_close_mode(sym), now)
    if history_days is not None:
        max_bars = min(max_bars, bars_for_days(interval, history_days))
    df, closes = df.iloc[-max_bars:], closes[-max_bars:]
    if len(df) < 30:
        raise ValueError(f"not enough closed {interval} candles ({len(df)})")
    htf = HTF.get(interval, "1d")
    try:
        hs = htf_trend_series(sym, htf, now, max(len(df) // 3, 600)) if htf != interval and with_htf \
            else pd.Series(dtype=float)
    except Exception:
        hs = pd.Series(dtype=float)
    return context_from_frame(sym, interval, df, closes, forming, series.source, series.delayed_minutes,
                              htf, hs, series.source_info)


def context_from_frame(sym: Optional[Symbol], interval: str, df: pd.DataFrame, closes: pd.DatetimeIndex,
                       forming: Optional[dict] = None, source: str = "synthetic", delayed_minutes: int = 0,
                       htf: Optional[str] = None, htf_trend: Optional[pd.Series] = None,
                       source_info: Optional[dict] = None) -> Context:
    """Build the causal structure state from CLOSED candles (no I/O; used by tests and the backtest)."""
    a = ind.atr(df, 14)
    atr_prev = a.shift(1).to_numpy(float)
    imp = impulse_flags(df, atr_prev)
    run = run_structure(df)
    if htf_trend is not None and len(htf_trend):
        aligned = pd.merge_asof(pd.DataFrame({"t": ns_index(closes)}),
                                pd.DataFrame({"t": ns_index(htf_trend.index), "v": htf_trend.values}),
                                on="t", direction="backward")["v"].fillna(0).to_numpy(float)
    else:
        aligned = np.zeros(len(df))
    return Context(sym, interval, source, delayed_minutes, df, closes, forming, a.to_numpy(float), atr_prev, imp,
                   run, htf or HTF.get(interval, "1d"), aligned, np.array([_TN[x] for x in run.trend], float),
                   source_info)


def analyze(sym: Symbol, interval: str = "1h", lookback: int = 300, left: int = 2, right: int = 2,
            now: Optional[pd.Timestamp] = None, with_sessions: bool = True) -> dict:
    now = now or now_ts()
    series = get_series(sym, interval)
    fx_day = day_close_mode(sym)
    full, closes_full, forming, _ = closed_frame(series.df, interval, fx_day, now)
    if len(full) < 30:
        raise ValueError(f"not enough closed {interval} candles ({len(full)})")
    atr_full = ind.atr(full, 14)
    atr_prev_full = atr_full.shift(1).to_numpy(float)
    start = max(0, len(full) - lookback)
    df = full.iloc[start:]
    closes = closes_full[start:]
    atr_prev = atr_prev_full[start:]
    imp = impulse_flags(df, atr_prev)
    run = run_structure(df, left, right)
    end = len(df) - 1
    atr_now = float(atr_full.iloc[-1])
    b = bodies(df)
    box = box_status(df, end, _b=b)
    prev_box = {"low": float(b[1][end]), "high": float(b[0][end]), "time": iso(df.index[end]),
                "width_atr": float((b[0][end] - b[1][end]) / atr_now)}

    def fmt_box(bx):
        if bx.get("status") == "none":
            return bx
        out = {"status": bx["status"], "low": bx["low"], "high": bx["high"],
               "width_atr": (bx["high"] - bx["low"]) / atr_now, "mother_time": iso(df.index[bx["mother_idx"]]),
               "n_candles": bx["n_inside"] + 1}
        if bx.get("direction"):
            out["break_direction"] = bx["direction"]
            out["breakout_time"] = iso(df.index[bx["breakout_idx"]])
            out["breakout_impulse"] = bool(imp[bx["breakout_idx"]])
            out["breakout_session"] = session_label(closes[bx["breakout_idx"]])
        if bx.get("failed_idx") is not None:
            out["failed_time"] = iso(df.index[bx["failed_idx"]])
        return out

    swings = [_swing_dict(s, df) for s in run.swings[-6:]]
    evs = [_event_dict(e, df, closes, atr_prev, imp) for e in run.events]
    bos = [e for e in evs if e["type"].startswith("bos")]
    recent_cut = closes[-1] - pd.Timedelta(hours=48)
    sweeps = [e for e in evs if e["type"].startswith("sweep") and pd.Timestamp(e["close_time"]) >= recent_cut]
    trend = run.trend[-1]
    last_bos = bos[-1] if bos else None
    prot_low = next((s for s in reversed(run.swings) if s.kind == "L"), None)
    prot_high = next((s for s in reversed(run.swings) if s.kind == "H"), None)
    impulses = [{"time": iso(df.index[i]), "direction": "up" if df["c"].iloc[i] > df["o"].iloc[i] else "down",
                 "body_atr": float(abs(df["c"].iloc[i] - df["o"].iloc[i]) / atr_prev[i]),
                 "session_at_close": session_label(closes[i])}
                for i in range(max(0, end - 9), end + 1) if imp[i]]
    nb = fmt_box(box)
    next_close = forming["close_time"] if forming else iso(next_close_after(closes[-1], interval, now,
                                                                           is_fx_like(sym)))
    # setup summary
    if nb.get("status") == "holding":
        state = "consolidation"
        long_t, short_t = nb["high"], nb["low"]
    elif nb.get("status") == "broken":
        state = f"breakout_{nb['break_direction']}"
        # continuation = next close beyond the last candle body; failure = close back inside the box
        if nb["break_direction"] == "up":
            long_t, short_t = prev_box["high"], nb["high"]
        else:
            long_t, short_t = nb["low"], prev_box["low"]
    elif nb.get("status") == "failed":
        state = f"failed_breakout_{nb['break_direction']}"
        long_t, short_t = nb["high"], nb["low"]
    else:
        state = {"up": "trending_up", "down": "trending_down"}.get(trend, "range")
        long_t = prot_high.price if prot_high and not prot_high.broken else prev_box["high"]
        short_t = prot_low.price if prot_low and not prot_low.broken else prev_box["low"]
    buf = STOP_BUFFER_ATR * atr_now
    setup = {
        "state": state, "trend": trend,
        "long_trigger": f"{interval} candle CLOSES above {long_t:.6g}",
        "short_trigger": f"{interval} candle CLOSES below {short_t:.6g}",
        "long_trigger_level": long_t, "short_trigger_level": short_t,
        "protected_low": prot_low.price if prot_low else None,
        "protected_high": prot_high.price if prot_high else None,
        "long_stop": (prot_low.price - buf) if prot_low else None,
        "short_stop": (prot_high.price + buf) if prot_high else None,
        "next_candle_close_utc": next_close,
        "next_close_session": session_label(pd.Timestamp(next_close)),
    }
    out = {
        "symbol": sym.id, "interval": interval, **series.source_info, "ticker": series.ticker,
        "as_of": iso(df.index[-1]), "last_closed_candle_close_utc": iso(closes[-1]),
        "delayed_minutes": series.delayed_minutes, "params": {"left": left, "right": right, "lookback": len(df),
                                                               "impulse_body_atr": IMPULSE_BODY_ATR},
        "atr14": atr_now, "last_close": float(df["c"].iloc[-1]), "forming_candle": forming,
        "trend": trend, "swings": swings, "last_bos": last_bos, "bos_events": bos[-5:],
        "sweeps_48h": sweeps[-8:], "box": nb, "prev_candle_box": prev_box, "recent_impulses": impulses,
        "setup": setup,
    }
    if with_sessions:
        try:
            hourly = series.df if INTERVALS[interval][4] <= 1 else get_series(sym, "1h").df
            out["session_levels"] = session_levels(hourly, now)
        except Exception as e:  # pragma: no cover
            out["session_levels"] = {"error": str(e)[:200]}
    return out


# ------------------------------------------------------------------ signals

def evaluate_last(df: pd.DataFrame, closes, atr_prev, imp, run: StructureRun, interval: str) -> list[dict]:
    """Signals fired by the LAST closed candle of `df`."""
    end = len(df) - 1
    b = bodies(df)
    c = df["c"].to_numpy(float)
    sigs = []
    for e in run.events:
        if e.idx == end:
            d = "up" if e.type in ("bos_up", "sweep_low") else "down"
            sigs.append({"type": e.type, "direction": d, "level": e.level, "swing_label": e.swing_label})
    prior = box_at(df, end - 1, _b=b)
    if prior:
        if c[end] > prior["high"]:
            sigs.append({"type": "box_breakout_up", "direction": "up", "level": prior["high"], "box": prior})
        elif c[end] < prior["low"]:
            sigs.append({"type": "box_breakout_down", "direction": "down", "level": prior["low"], "box": prior})
    st = box_status(df, end, _b=b)
    if st.get("status") == "failed" and st.get("failed_idx") == end:
        d = "down" if st["direction"] == "up" else "up"
        sigs.append({"type": "failed_breakout", "direction": d, "level": st["high"] if d == "down" else st["low"],
                     "box": {k: st[k] for k in ("low", "high", "mother_idx", "n_inside")},
                     "failed_break_direction": st["direction"]})
    for s in sigs:
        s["impulse"] = bool(imp[end])
        s["body_atr"] = float(abs(c[end] - df["o"].iloc[end]) / atr_prev[end]) if np.isfinite(atr_prev[end]) else None
    return sigs


def signal_plan(sig: dict, df: pd.DataFrame, run: StructureRun, atr: float) -> dict:
    end = len(df) - 1
    entry = float(df["c"].iloc[end])
    up = sig["direction"] == "up"
    buf = STOP_BUFFER_ATR * atr
    swings = [s for s in run.swings if s.confirmed_idx <= end]
    if up:
        prot = next((s for s in reversed(swings) if s.kind == "L" and s.price < entry), None)
        stop = (prot.price - buf) if prot else None
        nxt = sorted([s.price for s in swings if s.kind == "H" and s.price > entry])
    else:
        prot = next((s for s in reversed(swings) if s.kind == "H" and s.price > entry), None)
        stop = (prot.price + buf) if prot else None
        nxt = sorted([s.price for s in swings if s.kind == "L" and s.price < entry], reverse=True)
    risk = abs(entry - stop) if stop is not None else None
    targets = []
    min_dist = max(0.5 * (risk or 0), 0.25 * atr)
    nxt = [p for p in nxt if abs(p - entry) >= min_dist]
    if nxt:
        targets.append({"kind": "next_swing", "price": nxt[0]})
    box = sig.get("box")
    if box:
        w = box["high"] - box["low"]
        mm = (box["high"] + w) if up else (box["low"] - w)
        if sig["type"] == "failed_breakout":
            mm = box["low"] if not up else box["high"]  # opposite edge
            targets.append({"kind": "opposite_box_edge", "price": mm})
        else:
            targets.append({"kind": "measured_move_box_width", "price": mm})
    elif prot is not None and sig["type"].startswith("bos"):
        leg = abs(sig["level"] - prot.price)
        targets.append({"kind": "measured_move_leg", "price": sig["level"] + leg if up else sig["level"] - leg})
    if risk:
        targets.append({"kind": "1R", "price": entry + risk if up else entry - risk})
        for t in targets:
            t["r_multiple"] = round(abs(t["price"] - entry) / risk, 2)
    return {"entry_ref": entry, "stop": stop,
            "protected_swing": {"type": prot.kind, "label": prot.label, "body": prot.price,
                                "time": iso(df.index[prot.idx])} if prot else None,
            "risk": risk, "risk_atr": risk / atr if risk and atr else None, "targets": targets}


def signal_id(symbol: str, interval: str, sig_type: str, close_time) -> str:
    return f"{symbol}:{interval}:{sig_type}:{iso(pd.Timestamp(close_time))}"


def signals(sym: Symbol, intervals: list[str], now: Optional[pd.Timestamp] = None) -> dict:
    """Evaluate the latest CLOSED candle on each interval; MTF alignment vs the largest interval's trend."""
    from .model import continuation_probability, public_structure_model, structure_model

    now = now or now_ts()
    ivs = sorted(dict.fromkeys(intervals), key=lambda x: INTERVALS[x][4], reverse=True)
    out, errors, ctxs = {}, {}, {}
    extra_ctx: dict[str, Context] = {}
    for iv in ivs:
        try:
            ctxs[iv] = build_context(sym, iv, now)
        except Exception as e:
            errors[iv] = f"{type(e).__name__}: {str(e)[:200]}"
    top = ivs[0] if ivs else None
    fired_all = []
    for iv, ctx in ctxs.items():
        df, end = ctx.df, len(ctx.df) - 1
        atr = float(ctx.atr[end])
        trend = ctx.run.trend[-1]
        if iv == top and top in ctxs:
            ref_iv, ref_trend = ctx.htf, {1: "up", -1: "down", 0: "range"}[int(ctx.htf_trend[end])]
        else:
            ref_iv, ref_trend = top, ctxs[top].run.trend[-1] if top in ctxs else "range"
        sigs = evaluate_last(df, ctx.closes, ctx.atr_prev, ctx.imp, ctx.run, iv)
        model = None
        cc = ctx.closes[end]
        for sg in sigs:
            sg.update(signal_plan(sg, df, ctx.run, atr))
            d = 1 if sg["direction"] == "up" else -1
            tn = {"up": 1, "down": -1}.get(ref_trend, 0)
            sg["mtf"] = {"reference_interval": ref_iv, "reference_trend": ref_trend,
                         "alignment": "aligned" if tn == d else "counter" if tn == -d else "neutral"}
            sg["candle_time"] = iso(df.index[end])
            sg["candle_close_time"] = iso(cc)
            sg["session_at_close"] = session_label(cc)
            sg["id"] = signal_id(sym.id, iv, sg["type"], cc)
            if sg["type"] in ("bos_up", "bos_down", "box_breakout_up", "box_breakout_down"):
                try:
                    model = model or structure_model(sym, iv)
                    ev = {"t": end, "type": sg["type"], "dir": d, "level": sg["level"], "box": sg.get("box")}
                    sg.update(continuation_probability(model, ctx, ev))
                except Exception as e:
                    sg["continuation_probability"] = None
                    sg["model_error"] = str(e)[:200]
            else:
                sg["continuation_probability"] = None
                sg["model_note"] = "not modelled for this signal type"
            if "box" in sg and sg["box"]:
                bx = sg["box"]
                sg["box"] = {"low": bx["low"], "high": bx["high"], "n_candles": bx["n_inside"] + 1,
                             "width_atr": (bx["high"] - bx["low"]) / atr if atr else None}
        # range signals: wick sweep of a box edge that closed back inside / first arrival at an edge zone
        try:
            from .mtf import range_signals
            from .retest import default_pip as _dp
            for rs in range_signals(ctx, _dp(sym)):
                d = 1 if rs["direction"] == "up" else -1
                tn = {"up": 1, "down": -1}.get(ref_trend, 0)
                rs.update({
                    "impulse": bool(ctx.imp[end]),
                    "mtf": {"reference_interval": ref_iv, "reference_trend": ref_trend,
                            "alignment": "aligned" if tn == d else "counter" if tn == -d else "neutral"},
                    "candle_time": iso(df.index[end]), "candle_close_time": iso(cc),
                    "session_at_close": session_label(cc), "id": signal_id(sym.id, iv, rs["type"], cc),
                    "continuation_probability": None, "model_note": "see /mtf odds for historical rates",
                })
                sigs.append(rs)
        except Exception as e:
            errors[f"{iv}:range"] = f"{type(e).__name__}: {str(e)[:200]}"
        # break -> retest signals (levels from this interval and its higher timeframe)
        try:
            from .retest import default_pip, retest_signals
            lvl_iv = HTF.get(iv, iv)
            lvl_ctx = ctxs.get(lvl_iv)
            if lvl_ctx is None and lvl_iv != iv:
                lvl_ctx = extra_ctx.get(lvl_iv)
                if lvl_ctx is None:
                    lvl_ctx = extra_ctx[lvl_iv] = build_context(sym, lvl_iv, now)
            for rs in retest_signals(ctx, lvl_ctx, default_pip(sym)):
                d = 1 if rs["direction"] == "up" else -1
                tn = {"up": 1, "down": -1}.get(ref_trend, 0)
                rs.update({
                    "impulse": bool(ctx.imp[end]),
                    "body_atr": float(abs(df["c"].iloc[end] - df["o"].iloc[end]) / ctx.atr_prev[end])
                    if np.isfinite(ctx.atr_prev[end]) else None,
                    "mtf": {"reference_interval": ref_iv, "reference_trend": ref_trend,
                            "alignment": "aligned" if tn == d else "counter" if tn == -d else "neutral"},
                    "candle_time": iso(df.index[end]), "candle_close_time": iso(cc),
                    "session_at_close": session_label(cc), "id": signal_id(sym.id, iv, rs["type"], cc),
                    "continuation_probability": None, "model_note": "see /retest for historical statistics",
                })
                sigs.append(rs)
        except Exception as e:
            errors[f"{iv}:retest"] = f"{type(e).__name__}: {str(e)[:200]}"
        out[iv] = {
            "last_closed_candle": {"start": iso(df.index[end]), "close_time": iso(cc), "o": float(df["o"].iloc[end]),
                                   "h": float(df["h"].iloc[end]), "l": float(df["l"].iloc[end]),
                                   "c": float(df["c"].iloc[end]), "session_at_close": session_label(cc)},
            "candle_close_time": iso(cc), "trend": trend, "atr14": atr, "signals": sigs,
            "forming_candle": ctx.forming, **(ctx.source_info or {"source": ctx.source}),
            "delayed_minutes": ctx.delayed_minutes,
            "structure_model": {k: v for k, v in public_structure_model(model).items()
                                if k in ("status", "n_events", "base_hit_rate", "k_candles")} if model else None,
        }
        fired_all.extend(sigs)
    # level_touch: the last closed candle of the LOWEST interval interacted with a top zone
    try:
        if len(ctxs) >= 1:
            from .levels import last_candle_touches, levels_engine, touch_odds
            base_iv = ivs[-1]
            if base_iv in out:
                pub, raw = levels_engine(sym, list(ctxs))
                cc = raw["_last_close_time"]
                for tch in last_candle_touches(raw, pub):
                    z, kind = tch["zone"], tch["reaction"]
                    od = tch["odds"] or touch_odds(z, pub["statistics"], pub["intervals"], pub["now"]["session"])
                    support = tch["raw"]["role_tested"] == "support"
                    held = kind not in ("break", "retest_fail", "inside_zone")
                    conf = od["conditions"]["confluence"]
                    sg = {"type": "level_touch", "reaction": kind,
                          "direction": ("up" if support else "down") if held else ("down" if support else "up")
                          if kind != "inside_zone" else "undecided",
                          "level": z["level"], "zone_low": z["low"], "zone_high": z["high"],
                          "timeframes": z["timeframes"], "role_tested": tch["raw"]["role_tested"],
                          "respected_touches": z["respected_touches"], "flips": z["flips"],
                          "wick_depth_pips": tch["raw"]["wick_depth_pips"],
                          "hold_probability": conf.get("p_hold"), "hold_probability_ci95": conf.get("ci95_hold"),
                          "hold_probability_n": conf.get("n"), "hold_probability_basis": f"confluence = {conf['bucket']}",
                          "odds": od["conditions"], "odds_text": od["text"],
                          "candle_close_time": iso(cc), "session_at_close": session_label(cc),
                          "id": signal_id(sym.id, base_iv, f"level_touch:{z['level']:.6g}", cc),
                          "continuation_probability": None,
                          "model_note": "hold_probability = historical share of such touches that held (see /levels)"}
                    out[base_iv]["signals"].append(sg)
                    fired_all.append(sg)
    except Exception as e:
        errors["level_touch"] = f"{type(e).__name__}: {str(e)[:200]}"
    return {"symbol": sym.id, "evaluated_at": iso(now), "intervals": out, "fired": len(fired_all),
            "fired_ids": [s["id"] for s in fired_all], "errors": errors,
            **((next(iter(ctxs.values())).source_info or {}) if ctxs else {"source": None}),
            "as_of": max((v["candle_close_time"] for v in out.values()), default=None),
            "note": "Signals use CLOSED candles only; dedupe on `id` (symbol:interval:type:candle_close_time). "
                    "Not financial advice."}
