"""Currency strength from 5-minute candles of the seven USD majors (local M1 store + live tail).

For each currency (USD, EUR, GBP, JPY, AUD, CAD, CHF, NZD) and each window (1h, 4h, 1d):
  z_c   = the currency's log move against the dollar over the window / (5m volatility x sqrt(window bars))
          (5m volatility = rolling standard deviation of 5m log returns over the previous 20 trading days)
  s_c   = z_c - mean(z over the 8 currencies, with z_USD = 0)      -> average volatility-normalised move
                                                                      against the others
Causal: every value at time t uses candles closed at or before t. NaN where fewer than 5 of the 7 pairs have data.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from .cache import TTL_INTRADAY, cache
from .data import get_series, ns_index, replay_key
from .symbols import Symbol, normalize

USD_PAIRS = {"EUR": ("EURUSD", 1), "GBP": ("GBPUSD", 1), "AUD": ("AUDUSD", 1), "NZD": ("NZDUSD", 1),
             "JPY": ("USDJPY", -1), "CAD": ("USDCAD", -1), "CHF": ("USDCHF", -1)}
CURRENCIES = ("USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD")
WINDOWS = {"1h": 12, "4h": 48, "1d": 288}  # in 5m bars
VOL_BARS, VOL_MIN = 5760, 1000
MIN_PAIRS = 5
FFILL_BARS = 12
STRENGTH_FEATURES = [f"str_{k}_{w}" for w in WINDOWS for k in ("base", "quote", "diff")]


def applies(sym: Optional[Symbol]) -> bool:
    """Strength features exist for the majors' crosses and for spot metals."""
    if sym is None or len(sym.id) != 6:
        return False
    return sym.asset_class == "metal" or (sym.id[:3] in CURRENCIES and sym.id[3:] in CURRENCIES)


def _close_series(sym: Symbol) -> Optional[pd.Series]:
    try:
        df = get_series(sym, "5m").df
    except Exception:
        return None
    if df.empty:
        return None
    s = df["c"].copy()
    s.index = ns_index(df.index) + pd.Timedelta(minutes=5)  # value known at the candle close
    return s[s > 0]


def z_moves(log_price: pd.Series) -> dict[str, pd.Series]:
    """Volatility-normalised log move over each window (causal)."""
    r = log_price.diff()
    sigma = r.rolling(VOL_BARS, min_periods=VOL_MIN).std()
    return {w: (log_price - log_price.shift(n)) / (sigma * np.sqrt(n)) for w, n in WINDOWS.items()}


def strength_from_closes(closes: dict[str, pd.Series]) -> dict:
    """closes: currency -> 5m close series of its USD pair (already sign-agnostic). Returns the grid and, per
    window, an array (grid x 8 currencies) of strengths."""
    if not closes:
        return {"times": pd.DatetimeIndex([], tz="UTC"), "s": {}}
    grid = None
    for s in closes.values():
        grid = s.index if grid is None else grid.union(s.index)
    z = {w: np.full((len(grid), len(CURRENCIES)), np.nan) for w in WINDOWS}
    for w in WINDOWS:
        z[w][:, 0] = 0.0  # USD is the numeraire
    for cur, s in closes.items():
        sign = USD_PAIRS[cur][1]
        lp = np.log(s.reindex(grid).ffill(limit=FFILL_BARS)) * sign
        for w, zs in z_moves(lp).items():
            z[w][:, CURRENCIES.index(cur)] = zs.to_numpy()
    out = {}
    for w in WINDOWS:
        have = np.isfinite(z[w][:, 1:]).sum(axis=1)
        with np.errstate(invalid="ignore"):
            mean = np.nanmean(z[w], axis=1)
        s = z[w] - mean[:, None]
        s[have < MIN_PAIRS] = np.nan
        out[w] = s
    return {"times": ns_index(grid), "s": out}


def strength_table() -> dict:
    def load():
        closes = {}
        for cur, (pair, _) in USD_PAIRS.items():
            s = _close_series(normalize(pair))
            if s is not None and len(s) > VOL_MIN:
                closes[cur] = s
        t = strength_from_closes(closes)
        t["pairs"] = sorted(USD_PAIRS[c][0] for c in closes)
        return t
    return cache.get_or_set(("strength", replay_key()), 15 * 60, load)


def own_z(sym: Symbol) -> Optional[dict]:
    """Gold / silver: the metal's own volatility-normalised move against the dollar."""
    def load():
        s = _close_series(sym)
        if s is None or len(s) <= VOL_MIN:
            return None
        return {"times": ns_index(s.index), "z": {w: v.to_numpy() for w, v in z_moves(np.log(s)).items()}}
    return cache.get_or_set(("strength_own", sym.id, replay_key()), 15 * 60, load)


def strength_block(sym: Symbol, times: pd.DatetimeIndex, table: Optional[dict] = None,
                   own: Optional[dict] = None) -> np.ndarray:
    """Per row (row time = candle close): base strength, quote strength and their difference at 1h / 4h / 1d."""
    n = len(times)
    out = np.full((n, len(STRENGTH_FEATURES)), np.nan)
    if sym is None or n == 0:
        return out
    base, quote = (sym.id[:3], sym.id[3:]) if len(sym.id) == 6 else (None, None)
    metal = sym.asset_class == "metal"
    if not metal and (base not in CURRENCIES or quote not in CURRENCIES):
        return out
    table = table if table is not None else strength_table()
    g = table["times"]
    if len(g) == 0:
        return out
    t = ns_index(times)
    pos = g.searchsorted(t, side="right") - 1  # last grid point at or before the row
    ok = pos >= 0
    p = np.where(ok, pos, 0)
    if metal:
        own = own if own is not None else own_z(sym)
        if own is not None:
            po = own["times"].searchsorted(t, side="right") - 1
            oko = po >= 0
            pp = np.where(oko, po, 0)
    for i, w in enumerate(WINDOWS):
        s = table["s"][w]
        q = np.where(ok, s[p, CURRENCIES.index("USD" if metal else quote)], np.nan)
        if metal:
            b = np.where(oko, own["z"][w][pp], np.nan) if own is not None else np.full(n, np.nan)
        else:
            b = np.where(ok, s[p, CURRENCIES.index(base)], np.nan)
        out[:, 3 * i], out[:, 3 * i + 1], out[:, 3 * i + 2] = b, q, b - q
    return out
