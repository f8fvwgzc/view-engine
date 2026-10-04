"""Return-correlation analytics: matrix, rolling corr, regime-change flag, lead-lag, beta."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from .data import DataError, get_series
from .symbols import Symbol

REGIME_CHANGE_THRESHOLD = 0.3


def _corr(a: pd.Series, b: pd.Series) -> float | None:
    if len(a) < 5:
        return None
    v = a.corr(b)
    return None if pd.isna(v) else float(v)


def pair_stats(first: pd.Series, other: pd.Series, window: int) -> dict:
    """Stats for `other` vs `first` (both return series on a common index)."""
    out: dict = {"n_obs": int(len(first))}
    for w in (20, 60, 250):
        out[f"corr_{w}"] = _corr(first.iloc[-w:], other.iloc[-w:]) if len(first) >= w else None
    roll = first.rolling(60).corr(other).dropna()
    avg1y = float(roll.iloc[-252:].mean()) if len(roll) >= 60 else None
    out["corr_60_avg_1y"] = avg1y
    cur = out["corr_60"]
    out["regime_change"] = bool(cur is not None and avg1y is not None
                                and abs(cur - avg1y) >= REGIME_CHANGE_THRESHOLD)
    f, o = first.iloc[-window:], other.iloc[-window:]
    out["corr_window"] = _corr(f, o)
    # lead-lag on the window: other_{t-1} vs first_t  ("other leads") and first_{t-1} vs other_t
    out["lead_other_t-1_vs_first_t"] = _corr(o.shift(1).iloc[1:], f.iloc[1:])
    out["lead_first_t-1_vs_other_t"] = _corr(f.shift(1).iloc[1:], o.iloc[1:])
    var = float(o.var())
    out["beta_first_on_other"] = float(f.cov(o) / var) if var > 0 else None
    return out


def correlation_report(symbols: list[Symbol], interval: str = "1d", window: int = 60) -> dict:
    if len(symbols) < 2:
        raise ValueError("need at least 2 symbols")
    errors: dict[str, str] = {}
    closes: dict[str, pd.Series] = {}
    sources: dict[str, dict] = {}

    def load(s: Symbol):
        return s, get_series(s, interval)

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(load, s) for s in symbols]
        for f, s in zip(futs, symbols):
            try:
                _, ser = f.result()
                closes[s.id] = ser.df["c"]
                sources[s.id] = {"source": ser.source, "ticker": ser.ticker, "as_of": ser.as_of,
                                 "delayed_minutes": ser.delayed_minutes}
            except Exception as e:
                errors[s.id] = str(e)[:300]
    first = symbols[0].id
    if first not in closes:
        raise DataError(f"no data for base symbol {first}: {errors.get(first)}")
    others = [s.id for s in symbols[1:] if s.id in closes]
    if not others:
        raise DataError(f"no data for any comparison symbol: {errors}")
    # Rates: use differences (yield changes), everything else log returns.
    rate_ids = {s.id for s in symbols if s.asset_class == "rate"}
    rets_cols = {k: (v.diff() if (v <= 0).any() or k in rate_ids else np.log(v).diff())
                 for k, v in closes.items()}
    rets = pd.concat(rets_cols, axis=1, join="inner").dropna()
    if len(rets) < 10:
        raise DataError("fewer than 10 overlapping observations")
    w = rets.iloc[-window:]
    matrix = w.corr().round(3)
    vs_first = {o: pair_stats(rets[first], rets[o], window) for o in others}
    return {
        "base": first, "interval": interval, "window": window, "n_overlap": int(len(rets)),
        "window_start": rets.index[-len(w)], "window_end": rets.index[-1],
        "matrix": {r: {c: (None if pd.isna(matrix.loc[r, c]) else float(matrix.loc[r, c]))
                       for c in matrix.columns} for r in matrix.index},
        "vs_base": vs_first,
        "notes": "log returns (yield changes for rates) on inner-joined timestamps; regime_change when "
                 f"|corr_60 - 1y avg of rolling corr_60| >= {REGIME_CHANGE_THRESHOLD}; "
                 "beta = cov(base, other)/var(other) over window (for rates: per 1.00 = 100bp yield change)",
        "sources": sources, "errors": errors,
    }

