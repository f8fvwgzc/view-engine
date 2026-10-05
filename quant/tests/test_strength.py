import numpy as np
import pandas as pd
import pytest

from app import strength as ST
from app.symbols import normalize


def series(n=4000, drift=0.0, seed=0, base=1.0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-03-03 00:05", periods=n, freq="5min", tz="UTC")
    r = rng.normal(0, 0.0003, n) + drift
    return pd.Series(base * np.exp(np.cumsum(r)), index=idx)


def closes(eur_drift=0.0, jpy_drift=0.0, n=4000):
    out = {c: series(n, seed=i + 1) for i, c in enumerate(("GBP", "AUD", "NZD", "CAD", "CHF"))}
    out["EUR"] = series(n, drift=eur_drift, seed=11)             # EURUSD rising = EUR strong
    out["JPY"] = series(n, drift=jpy_drift, seed=12, base=150)   # USDJPY rising = JPY weak
    return out


def test_strength_sign_and_normalisation():
    t = ST.strength_from_closes(closes(eur_drift=0.00008, jpy_drift=0.00008))
    s = t["s"]["1d"][-1]
    cur = dict(zip(ST.CURRENCIES, s))
    assert cur["EUR"] > 1.0 and cur["JPY"] < -1.0                 # EUR strongest, JPY weakest
    assert abs(np.nansum(s)) < 1e-9                              # strengths are relative: they sum to zero
    assert np.isnan(t["s"]["1d"][:ST.VOL_MIN]).all()              # no volatility estimate yet -> NaN
    # values are volatility-normalised: scaling a pair's price changes nothing
    a = ST.z_moves(np.log(series(seed=3)))["4h"].to_numpy()
    b = ST.z_moves(np.log(series(seed=3) * 123.0))["4h"].to_numpy()
    np.testing.assert_allclose(np.nan_to_num(a), np.nan_to_num(b), atol=1e-9)


def test_strength_block_pairs_gold_and_missing_pairs():
    table = ST.strength_from_closes(closes(eur_drift=0.00008))
    times = table["times"][[-1, -500, 10]]
    blk = ST.strength_block(normalize("EURUSD"), times, table)
    row = dict(zip(ST.STRENGTH_FEATURES, blk[0]))
    assert row["str_base_1d"] > 0 and row["str_diff_1d"] == pytest.approx(row["str_base_1d"] - row["str_quote_1d"])
    assert np.isnan(blk[2]).all()                                 # before the volatility warm-up
    jp = dict(zip(ST.STRENGTH_FEATURES, ST.strength_block(normalize("EURJPY"), times, table)[0]))
    assert jp["str_base_1d"] == pytest.approx(row["str_base_1d"])  # same EUR strength, different quote
    gold = series(seed=7, drift=0.0001, base=4000)
    own = {"times": gold.index, "z": {w: v.to_numpy() for w, v in ST.z_moves(np.log(gold)).items()}}
    g = dict(zip(ST.STRENGTH_FEATURES, ST.strength_block(normalize("XAUUSD"), times, table, own)[0]))
    usd = dict(zip(ST.CURRENCIES, table["s"]["1d"][-1]))["USD"]
    assert g["str_quote_1d"] == pytest.approx(usd) and g["str_base_1d"] > 1.0   # gold's own move vs USD strength
    assert np.isnan(ST.strength_block(normalize("SPX"), times, table)).all()
    few = {k: v for k, v in closes().items() if k in ("EUR", "GBP")}             # only 2 of 7 pairs
    assert np.isnan(ST.strength_from_closes(few)["s"]["1h"]).all()
    assert ST.applies(normalize("GBPJPY")) and ST.applies(normalize("gold")) and not ST.applies(normalize("SPY"))


def test_strength_is_causal():
    full = closes(eur_drift=0.00005)
    T = 3000
    cut = {k: v.iloc[:T] for k, v in full.items()}
    a, b = ST.strength_from_closes(full), ST.strength_from_closes(cut)
    for w in ST.WINDOWS:
        np.testing.assert_allclose(np.nan_to_num(a["s"][w][:T]), np.nan_to_num(b["s"][w]), atol=1e-12)
