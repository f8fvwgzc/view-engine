"""Technical indicators + structure (swings, S/R zones, pivots, regime). Pure pandas/numpy."""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    """Wilder RSI."""
    d = s.diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    au = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    ad = dn.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = au / ad.replace(0.0, np.nan)
    out = 100 - 100 / (1 + rs)
    out = out.where(ad != 0, 100.0)  # no losses -> RSI 100
    out[(ad == 0) & (au == 0)] = 50.0  # perfectly flat
    out[au.isna()] = np.nan
    return out


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["c"].shift(1)
    tr = pd.concat([df["h"] - df["l"], (df["h"] - pc).abs(), (df["l"] - pc).abs()], axis=1).max(axis=1)
    tr.iloc[0] = df["h"].iloc[0] - df["l"].iloc[0]
    return tr


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder ATR."""
    return true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def macd(s: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    m = ema(s, fast) - ema(s, slow)
    sig = m.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": m, "signal": sig, "hist": m - sig})


def bollinger(s: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(s, n)
    sd = s.rolling(n, min_periods=n).std(ddof=0)
    up, lo = mid + k * sd, mid - k * sd
    return pd.DataFrame({"mid": mid, "upper": up, "lower": lo,
                         "pct_b": (s - lo) / (up - lo), "bandwidth": (up - lo) / mid})


def bars_per_year(index: pd.DatetimeIndex) -> float:
    """Empirical bars/year from the timestamps (works for 24x5 FX, 6.5h equities, etc.)."""
    if len(index) < 3:
        return 252.0
    span_days = (index[-1] - index[0]).total_seconds() / 86400
    if span_days <= 0:
        return 252.0
    return (len(index) - 1) / (span_days / 365.25)


def realized_vol(s: pd.Series, n: int = 20, periods_per_year: Optional[float] = None) -> pd.Series:
    r = np.log(s).diff()
    ppy = periods_per_year or bars_per_year(s.index)
    return r.rolling(n, min_periods=n).std() * np.sqrt(ppy)


def trend_regime(close: pd.Series) -> dict:
    """Rule: UP if close > SMA50 > SMA200 and SMA50 rising over 10 bars; DOWN if mirror; else RANGE.
    Falls back to SMA20/SMA50 when fewer than 200 bars."""
    fast_n, slow_n = (50, 200) if len(close) >= 210 else (20, 50)
    f, sl = sma(close, fast_n), sma(close, slow_n)
    if len(close) < slow_n + 11 or np.isnan(sl.iloc[-1]):
        return {"regime": "unknown", "rule": "insufficient history"}
    c, fv, sv = close.iloc[-1], f.iloc[-1], sl.iloc[-1]
    slope = (fv - f.iloc[-11]) / f.iloc[-11]
    if c > fv > sv and slope > 0:
        reg = "up"
    elif c < fv < sv and slope < 0:
        reg = "down"
    else:
        reg = "range"
    return {"regime": reg, "rule": f"close vs SMA{fast_n} vs SMA{slow_n} + SMA{fast_n} 10-bar slope",
            "sma_fast_slope_10bar_pct": round(slope * 100, 3)}


def classic_pivots(h: float, l: float, c: float) -> dict:
    p = (h + l + c) / 3
    return {"P": p, "R1": 2 * p - l, "S1": 2 * p - h, "R2": p + (h - l), "S2": p - (h - l),
            "R3": h + 2 * (p - l), "S3": l - 2 * (h - p)}


def swing_points(df: pd.DataFrame, k: int = 3) -> tuple[pd.Series, pd.Series]:
    """Fractal swings: bar high is the max of the 2k+1 window centred on it (strictly unique-ish)."""
    w = 2 * k + 1
    hi_max = df["h"].rolling(w, center=True, min_periods=w).max()
    lo_min = df["l"].rolling(w, center=True, min_periods=w).min()
    highs = df["h"][(df["h"] == hi_max)]
    lows = df["l"][(df["l"] == lo_min)]
    # de-duplicate equal adjacent extremes
    highs = highs[~highs.index.duplicated()]
    lows = lows[~lows.index.duplicated()]
    return highs, lows


def cluster_levels(prices: np.ndarray, tol: float) -> list[dict]:
    """Greedy 1-D clustering on sorted prices; a cluster's total span never exceeds `tol`."""
    if len(prices) == 0:
        return []
    ps = np.sort(np.asarray(prices, dtype=float))
    clusters: list[list[float]] = [[ps[0]]]
    for p in ps[1:]:
        if p - clusters[-1][0] <= tol:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    return [{"level": float(np.mean(c)), "touches": len(c), "low": float(min(c)), "high": float(max(c))}
            for c in clusters]


def sr_zones(df: pd.DataFrame, atr_value: float, lookback: int = 300, k: int = 3, top: int = 5) -> dict:
    sub = df.iloc[-lookback:]
    highs, lows = swing_points(sub, k)
    last = float(df["c"].iloc[-1])
    if not np.isfinite(atr_value) or atr_value <= 0:
        atr_value = float((sub["h"] - sub["l"]).mean()) or abs(last) * 0.01
    pts = np.concatenate([highs.values, lows.values])
    zones = cluster_levels(pts, tol=0.5 * atr_value)  # each zone at most 0.5 ATR wide
    for z in zones:
        z["distance_atr"] = round((z["level"] - last) / atr_value, 2)
    sup = sorted([z for z in zones if z["level"] < last], key=lambda z: last - z["level"])[:top]
    res = sorted([z for z in zones if z["level"] >= last], key=lambda z: z["level"] - last)[:top]
    return {"support": sup, "resistance": res, "method": f"fractal swings (k={k}) over last {len(sub)} bars "
            f"clustered within 0.5*ATR; nearest first"}


def recent_swings(df: pd.DataFrame, k: int = 3, n: int = 5) -> dict:
    highs, lows = swing_points(df.iloc[-300:], k)
    return {"highs": [{"t": t, "price": float(v)} for t, v in highs.iloc[-n:].items()],
            "lows": [{"t": t, "price": float(v)} for t, v in lows.iloc[-n:].items()]}


PIVOT_BASIS = {"5m": "1d", "15m": "1d", "30m": "1d", "1h": "1d", "4h": "1d", "1d": "1wk", "1wk": "1mo", "1mo": "1mo"}
_PERIOD = {"1d": pd.Timedelta(days=1), "1wk": pd.Timedelta(days=7)}


def last_completed_bar(df: pd.DataFrame, interval: str, now: pd.Timestamp) -> Optional[pd.Series]:
    """Most recent bar whose period has ended (daily/weekly/monthly bars stamped at period start)."""
    if len(df) < 2:
        return None
    last_t = df.index[-1]
    if interval == "1mo":
        done = (now.year, now.month) != (last_t.year, last_t.month)
    elif interval == "1wk":  # trading week ends Friday; complete from Saturday 00:00 UTC
        monday = last_t.normalize() - pd.Timedelta(days=last_t.dayofweek)
        done = now >= monday + pd.Timedelta(days=5)
    else:
        done = now >= last_t + _PERIOD.get(interval, pd.Timedelta(days=1))
    return df.iloc[-1] if done else df.iloc[-2]


def analyze_frame(df: pd.DataFrame, interval: str, daily: Optional[pd.DataFrame] = None,
                  higher: Optional[pd.DataFrame] = None, now: Optional[pd.Timestamp] = None) -> dict:
    """Full indicator pack for one timeframe. `daily` -> 52w range; `higher` -> pivots."""
    now = now or pd.Timestamp.now(tz="UTC")
    c = df["c"]
    last = float(c.iloc[-1])
    a = atr(df, 14)
    atr_v = float(a.iloc[-1]) if len(a) and np.isfinite(a.iloc[-1]) else float("nan")
    m = macd(c)
    bb = bollinger(c)
    rv = realized_vol(c, 20)
    out: dict = {
        "last": last,
        "last_time": df.index[-1],
        "change_pct_1bar": float((c.iloc[-1] / c.iloc[-2] - 1) * 100) if len(c) > 1 else None,
        "atr14": atr_v,
        "atr14_pct": atr_v / last * 100 if last else None,
        "rsi14": float(rsi(c, 14).iloc[-1]),
        "sma20": float(sma(c, 20).iloc[-1]), "sma50": float(sma(c, 50).iloc[-1]),
        "sma200": float(sma(c, 200).iloc[-1]), "ema21": float(ema(c, 21).iloc[-1]),
        "macd": {k: float(v) for k, v in m.iloc[-1].items()},
        "bollinger": {k: float(v) for k, v in bb.iloc[-1].items()},
        "realized_vol_20_ann_pct": float(rv.iloc[-1] * 100),
        "bars_per_year_est": round(bars_per_year(df.index), 1),
        "trend": trend_regime(c),
        "swings": recent_swings(df),
        "levels": sr_zones(df, atr_v),
    }
    for k in ("sma20", "sma50", "sma200", "ema21"):
        v = out[k]
        out[f"dist_{k}_atr"] = (last - v) / atr_v if np.isfinite(v) and atr_v and np.isfinite(atr_v) else None
    if higher is not None and len(higher) >= 2:
        basis = PIVOT_BASIS.get(interval, "1d")
        bar = last_completed_bar(higher, basis, now)
        if bar is not None:
            pv = classic_pivots(float(bar["h"]), float(bar["l"]), float(bar["c"]))
            out["pivots"] = {"basis": f"previous completed {basis} bar ({bar.name.date()})", **pv}
    if daily is not None and len(daily) > 20:
        yr = daily[daily.index >= daily.index[-1] - pd.Timedelta(days=365)]
        hi, lo = float(yr["h"].max()), float(yr["l"].min())
        out["range_52w"] = {"high": hi, "low": lo,
                            "position_pct": (last - lo) / (hi - lo) * 100 if hi > lo else None,
                            "from_high_pct": (last / hi - 1) * 100, "from_low_pct": (last / lo - 1) * 100}
    return out
