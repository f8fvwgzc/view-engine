import numpy as np
import pandas as pd

from app import options as op
from app.symbols import OptionsProxy


def chain():
    strikes = np.array([90, 95, 100, 105, 110], float)
    calls = pd.DataFrame({"strike": strikes, "openInterest": [10, 20, 100, 300, 50],
                          "volume": [1, 2, 3, 4, 5], "impliedVolatility": [0.3] * 5})
    puts = pd.DataFrame({"strike": strikes, "openInterest": [400, 200, 100, 10, 5],
                         "volume": [5, 4, 3, 2, 1], "impliedVolatility": [0.3] * 5})
    return calls, puts


def test_max_pain_bruteforce():
    calls, puts = chain()
    mp = op.max_pain(calls, puts)

    def pain(k):
        return sum(max(k - s, 0) * oi for s, oi in zip(calls.strike, calls.openInterest)) + \
            sum(max(s - k, 0) * oi for s, oi in zip(puts.strike, puts.openInterest))
    assert mp == min(calls.strike, key=pain)
    assert mp == 100.0


def test_bs_gamma_matches_closed_form_and_finite_difference():
    S, K, iv, T, r = 100.0, 100.0, 0.2, 0.25, 0.04
    g = op.bs_gamma(S, np.array([K]), np.array([iv]), T, r)[0]
    from scipy.stats import norm
    d1 = (np.log(S / K) + (r + 0.5 * iv ** 2) * T) / (iv * np.sqrt(T))
    assert abs(g - norm.pdf(d1) / (S * iv * np.sqrt(T))) < 1e-12

    def call(s):
        d1 = (np.log(s / K) + (r + 0.5 * iv ** 2) * T) / (iv * np.sqrt(T))
        d2 = d1 - iv * np.sqrt(T)
        return s * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    h = 0.01
    fd = (call(S + h) - 2 * call(S) + call(S - h)) / h ** 2
    assert abs(g - fd) < 1e-4
    assert op.bs_gamma(S, np.array([K]), np.array([0.0]), T)[0] == 0.0


def test_gex_sign_and_scale():
    calls, puts = chain()
    c, p = op._prep(calls), op._prep(puts)
    gex = op.gex_by_strike(c, p, 100.0, 30 / 365)
    g100 = op.bs_gamma(100.0, np.array([100.0]), np.array([0.3]), 30 / 365)[0]
    assert abs(gex[100.0] - 0.0) < 1e-6  # equal call/put OI at 100 cancels
    g105 = op.bs_gamma(100.0, np.array([105.0]), np.array([0.3]), 30 / 365)[0]
    assert abs(gex[105.0] - g105 * (300 - 10) * 100 * 100 ** 2 * 0.01) < 1e-6
    assert gex[90.0] < 0 and gex[105.0] > 0
    assert g100 > g105


def test_gamma_flip_interpolation():
    gex = pd.Series([-10.0, -5.0, 20.0, 5.0], index=[90.0, 95.0, 100.0, 105.0])
    # cumsum: -10, -15, 5, 10 -> crosses between 95 and 100 at 95 + 5*15/20 = 98.75
    assert abs(op.gamma_flip(gex, 100.0) - 98.75) < 1e-9
    assert op.gamma_flip(pd.Series([1.0, 2.0], index=[1.0, 2.0])) is None


def test_analyze_chain():
    calls, puts = chain()
    res = op.analyze_chain(calls, puts, 100.0, 30 / 365)
    assert res["max_pain"] == 100.0
    assert res["call_walls"][0]["strike"] == 105.0 and res["put_walls"][0]["strike"] == 90.0
    assert abs(res["put_call_oi_ratio"] - 715 / 480) < 1e-9
    assert abs(res["atm_iv"] - 0.3) < 1e-12
    assert abs(res["skew_put5_minus_call5"]) < 1e-12


def test_translate_levels():
    direct = OptionsProxy("GLD", "direct")
    assert abs(op.translate_level(220.0, 200.0, 2000.0, direct) - 2200.0) < 1e-9
    inv = OptionsProxy("FXY", "inverse", "reciprocal")
    # FXY 60 <-> USDJPY 150 ; FXY up 10% -> USDJPY down to 150/1.1
    assert abs(op.translate_level(66.0, 60.0, 150.0, inv) - 150 / 1.1) < 1e-9
    dur = OptionsProxy("TLT", "inverse", "duration", 16.5)
    assert op.translate_level(101.65, 100.0, 4.0, dur) < 4.0
