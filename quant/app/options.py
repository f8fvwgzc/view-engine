"""Listed-options positioning (Yahoo option chains): OI walls, max pain, IV/skew, dealer gamma (GEX)."""
from __future__ import annotations

import math
from datetime import datetime, time, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy.stats import norm

from .cache import TTL_OPTIONS, cache
from .data import DataError, get_price, iso, price_for_ticker
from .symbols import OptionsProxy, Symbol

RISK_FREE = 0.04  # flat assumption for Black-Scholes gamma
CONTRACT_MULT = 100
NY = ZoneInfo("America/New_York")
IV_MIN, IV_MAX = 0.01, 5.0


# ------------------------------------------------------------------ pure math (unit-tested)

def bs_gamma(spot: float, strike: np.ndarray, iv: np.ndarray, t_years: float, r: float = RISK_FREE) -> np.ndarray:
    strike = np.asarray(strike, float)
    iv = np.asarray(iv, float)
    t = max(t_years, 1e-4)
    with np.errstate(divide="ignore", invalid="ignore"):
        d1 = (np.log(spot / strike) + (r + 0.5 * iv ** 2) * t) / (iv * math.sqrt(t))
        g = norm.pdf(d1) / (spot * iv * math.sqrt(t))
    return np.where((iv > 0) & np.isfinite(g), g, 0.0)


def max_pain(calls: pd.DataFrame, puts: pd.DataFrame) -> Optional[float]:
    """Strike minimising total intrinsic value paid to option holders at expiry."""
    strikes = np.union1d(calls["strike"].values, puts["strike"].values)
    if len(strikes) == 0:
        return None
    ck, coi = calls["strike"].values, calls["openInterest"].fillna(0).values
    pk, poi = puts["strike"].values, puts["openInterest"].fillna(0).values
    if coi.sum() + poi.sum() <= 0:
        return None
    pain = [(np.maximum(k - ck, 0) * coi).sum() + (np.maximum(pk - k, 0) * poi).sum() for k in strikes]
    return float(strikes[int(np.argmin(pain))])


def gex_by_strike(calls: pd.DataFrame, puts: pd.DataFrame, spot: float, t_years: float) -> pd.Series:
    """Dealer net gamma $ per 1% move per strike: gamma*OI*100*S^2*0.01, calls +, puts - (naive
    assumption: dealers long calls / short puts to customers -> standard 'GEX' convention)."""
    def part(df: pd.DataFrame, sign: float) -> pd.Series:
        if df.empty:
            return pd.Series(dtype=float)
        g = bs_gamma(spot, df["strike"].values, df["iv"].values, t_years)
        v = sign * g * df["openInterest"].fillna(0).values * CONTRACT_MULT * spot ** 2 * 0.01
        return pd.Series(v, index=df["strike"].values).groupby(level=0).sum()
    c, p = part(calls, 1.0), part(puts, -1.0)
    return c.add(p, fill_value=0.0).sort_index()


def gamma_flip(gex: pd.Series, spot: Optional[float] = None) -> Optional[float]:
    """Strike where cumulative (ascending strike) net GEX changes sign; linear interpolation.
    If several crossings, the one nearest spot."""
    if gex.empty:
        return None
    cum = gex.sort_index().cumsum()
    ks, vs = cum.index.values.astype(float), cum.values
    xs = []
    for i in range(1, len(vs)):
        if vs[i - 1] == 0:
            continue
        if np.sign(vs[i - 1]) != np.sign(vs[i]) and vs[i] != 0:
            k0, k1, v0, v1 = ks[i - 1], ks[i], vs[i - 1], vs[i]
            xs.append(float(k0 + (k1 - k0) * (-v0) / (v1 - v0)))
    if not xs:
        return None
    if spot is None:
        return xs[0]
    return min(xs, key=lambda x: abs(x - spot))


def _avg_iv(df: pd.DataFrame, lo: float, hi: float) -> Optional[float]:
    sub = df[(df["strike"] >= lo) & (df["strike"] <= hi) & df["iv"].between(IV_MIN, IV_MAX)]
    return float(sub["iv"].mean()) if len(sub) else None


def analyze_chain(calls: pd.DataFrame, puts: pd.DataFrame, spot: float, t_years: float, top: int = 5) -> dict:
    calls, puts = _prep(calls), _prep(puts)
    coi, poi = float(calls["openInterest"].sum()), float(puts["openInterest"].sum())
    cvol, pvol = float(calls["volume"].sum()), float(puts["volume"].sum())

    def walls(df):
        w = df[df["openInterest"] > 0].nlargest(top, "openInterest")
        return [{"strike": float(r.strike), "oi": int(r.openInterest), "pct_from_spot": (r.strike / spot - 1) * 100}
                for r in w.itertuples()]
    # ATM IV: average call/put IV at the strike(s) closest to spot
    strikes = np.union1d(calls["strike"].values, puts["strike"].values)
    atm_iv = None
    if len(strikes):
        k = strikes[np.argmin(np.abs(strikes - spot))]
        ivs = pd.concat([calls.loc[calls["strike"] == k, "iv"], puts.loc[puts["strike"] == k, "iv"]])
        ivs = ivs[ivs.between(IV_MIN, IV_MAX)]
        atm_iv = float(ivs.mean()) if len(ivs) else None
    put_iv = _avg_iv(puts, spot * 0.93, spot * 0.97)
    call_iv = _avg_iv(calls, spot * 1.03, spot * 1.07)
    gex = gex_by_strike(calls, puts, spot, t_years)
    near = gex[(gex.index >= spot * 0.9) & (gex.index <= spot * 1.1)]
    top_gex = near.reindex(near.abs().sort_values(ascending=False).index[:top])
    return {
        "total_call_oi": coi, "total_put_oi": poi, "total_call_volume": cvol, "total_put_volume": pvol,
        "put_call_oi_ratio": poi / coi if coi else None, "put_call_volume_ratio": pvol / cvol if cvol else None,
        "max_pain": max_pain(calls, puts),
        "call_walls": walls(calls), "put_walls": walls(puts),
        "atm_iv": atm_iv,
        "expected_move_1sd": spot * atm_iv * math.sqrt(t_years) if atm_iv else None,
        "skew_put5_minus_call5": (put_iv - call_iv) if put_iv is not None and call_iv is not None else None,
        "net_gex_total": float(gex.sum()),
        "gex_top_strikes": [{"strike": float(k), "net_gex": float(v)} for k, v in top_gex.items()],
        "gamma_flip_strike": gamma_flip(gex, spot),
    }


def _prep(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "impliedVolatility" in df:
        df["iv"] = pd.to_numeric(df["impliedVolatility"], errors="coerce")
    df["iv"] = df.get("iv", pd.Series(np.nan, index=df.index)).where(lambda s: s.between(IV_MIN, IV_MAX))
    for c in ("openInterest", "volume", "strike"):
        df[c] = pd.to_numeric(df.get(c, 0), errors="coerce").fillna(0.0)
    return df


# ------------------------------------------------------------------ proxy translation

def translate_level(level: float, proxy_px: float, under_px: float, px: OptionsProxy) -> float:
    if px.model == "reciprocal":
        return under_px * proxy_px / level
    if px.model == "duration":
        d = px.duration or 16.0
        return under_px - (level / proxy_px - 1) / d * 100.0
    return level * under_px / proxy_px


CAVEATS = {
    "direct": "Proxy levels scaled by the current underlying/proxy price ratio; tracking error, fees, "
              "carry and roll make translated levels approximate (zones, not exact prices).",
    "reciprocal": "Inverse FX ETF: underlying ~ k / ETF price, so proxy CALL walls map BELOW the pair and PUT walls "
                  "ABOVE; ETF option OI is small vs. the OTC FX options market (thin, low-confidence signal).",
    "duration": "TLT->10Y yield via duration approximation (D~16.5): bond-price levels map inversely to yields; "
                "TLT holds 20y+ bonds, so 10Y translation is rough.",
}


# ------------------------------------------------------------------ fetch + assemble

def _expiry_time(exp: str) -> datetime:
    d = datetime.strptime(exp, "%Y-%m-%d").date()
    return datetime.combine(d, time(16, 0), NY).astimezone(timezone.utc)


def _chain(ticker: str, exp: str):
    import yfinance as yf

    def load():
        ch = yf.Ticker(ticker).option_chain(exp)
        return ch.calls, ch.puts
    return cache.get_or_set(("chain", ticker, exp), TTL_OPTIONS, load)


def _expiries(ticker: str) -> list[str]:
    import yfinance as yf
    return cache.get_or_set(("expiries", ticker), TTL_OPTIONS, lambda: list(yf.Ticker(ticker).options or []))


def get_options(sym: Symbol, n_expiries: int = 3) -> dict:
    ticker = sym.options_ticker
    if not ticker:
        raise DataError(f"{sym.id} has no listed options or options proxy")
    exps = _expiries(ticker)
    now = datetime.now(timezone.utc)
    exps = [e for e in exps if _expiry_time(e) > now][: max(1, min(n_expiries, 8))]
    if not exps:
        raise DataError(f"no option expiries listed for {ticker}")
    pq = price_for_ticker(ticker)
    spot = pq["price"]
    proxy = sym.options_proxy
    under_px = None
    if proxy:
        try:
            under_px = get_price(sym)["price"]
        except Exception:
            under_px = None
    expiries, errors = [], {}
    agg_gex = pd.Series(dtype=float)
    for e in exps:
        try:
            calls, puts = _chain(ticker, e)
            t_years = max((_expiry_time(e) - now).total_seconds() / (365.25 * 86400), 1 / (365.25 * 24))
            res = analyze_chain(calls, puts, spot, t_years)
            agg_gex = agg_gex.add(gex_by_strike(_prep(calls), _prep(puts), spot, t_years), fill_value=0.0)
            res = {"expiry": e, "days_to_expiry": round(t_years * 365.25, 2), **res}
            if proxy and under_px:
                res["underlying_levels"] = _translate(res, spot, under_px, proxy)
            expiries.append(res)
        except Exception as ex:
            errors[e] = str(ex)[:200]
    if not expiries:
        raise DataError(f"option chains unavailable for {ticker}: {errors}")
    agg = {"net_gex_total": float(agg_gex.sum()), "gamma_flip_strike": gamma_flip(agg_gex, spot),
           "gamma_regime": "positive (dealers long gamma: mean-reverting / dampened moves)"
           if agg_gex.sum() > 0 else "negative (dealers short gamma: trend-amplifying / higher vol)"}
    if proxy and under_px and agg["gamma_flip_strike"]:
        agg["gamma_flip_underlying"] = translate_level(agg["gamma_flip_strike"], spot, under_px, proxy)
    return {
        "symbol": sym.id, "options_ticker": ticker,
        "proxy": {"ticker": proxy.ticker, "relation": proxy.relation, "model": proxy.model} if proxy else None,
        "spot": spot, "underlying_price": under_px, "source": "Yahoo Finance option chains (yfinance)",
        "as_of": pq["as_of"], "delayed_minutes": 15,
        "assumptions": f"BS gamma with r={RISK_FREE}, q=0, per-contract IV from Yahoo; GEX = gamma*OI*100*S^2*0.01 "
                       "($ per 1% move), calls +, puts - (convention: dealers long calls / short puts). This is a model "
                       "convention, not observed dealer positioning. OI updates once daily (prior close).",
        "aggregate": agg, "expiries": expiries, "errors": errors,
        "caveat": CAVEATS.get(proxy.model if proxy and proxy.relation == "inverse" else "direct") if proxy else
        "Direct listed options. OI is as of the prior session close.",
    }


def _translate(res: dict, proxy_px: float, under_px: float, px: OptionsProxy) -> dict:
    tr = lambda v: translate_level(v, proxy_px, under_px, px) if v else None  # noqa: E731
    inv = px.relation == "inverse"
    return {
        "max_pain": tr(res["max_pain"]),
        "gamma_flip": tr(res["gamma_flip_strike"]),
        "call_walls_mapped": [tr(w["strike"]) for w in res["call_walls"]],
        "put_walls_mapped": [tr(w["strike"]) for w in res["put_walls"]],
        "direction_note": ("inverse proxy: proxy call strikes correspond to LOWER underlying levels, put strikes "
                           "to HIGHER ones" if inv else "direct proxy: same direction"),
        "expected_move_1sd": _em(res["expected_move_1sd"], proxy_px, under_px, px),
    }


def _em(em: Optional[float], proxy_px: float, under_px: float, px: OptionsProxy) -> Optional[float]:
    if not em:
        return None
    if px.model == "duration":
        return em / proxy_px / (px.duration or 16.0) * 100.0  # yield points
    return em / proxy_px * under_px
