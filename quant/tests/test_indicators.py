import numpy as np
import pandas as pd

from app import indicators as ind


def frame(close, spread=0.5):
    idx = pd.date_range("2024-01-01", periods=len(close), freq="D", tz="UTC")
    c = pd.Series(close, index=idx, dtype=float)
    return pd.DataFrame({"o": c.shift(1).fillna(c.iloc[0]), "h": c + spread, "l": c - spread, "c": c, "v": 0.0})


def test_sma_ema():
    s = pd.Series(np.arange(1, 11, dtype=float))
    assert ind.sma(s, 5).iloc[-1] == 8.0
    assert np.isnan(ind.sma(s, 5).iloc[3])
    e = ind.ema(pd.Series([5.0] * 30), 10)
    assert abs(e.iloc[-1] - 5.0) < 1e-12


def test_rsi_extremes_and_flat():
    up = pd.Series(np.arange(100, dtype=float))
    assert ind.rsi(up).iloc[-1] == 100.0
    dn = pd.Series(np.arange(100, 0, -1, dtype=float))
    assert ind.rsi(dn).iloc[-1] < 1e-9
    flat = pd.Series([3.0] * 50)
    assert ind.rsi(flat).iloc[-1] == 50.0
    zig = pd.Series([1.0, 2.0] * 50)
    assert 40 < ind.rsi(zig).iloc[-1] < 60


def test_atr_constant_range():
    df = frame([100.0] * 60, spread=1.0)  # true range = 2 every bar
    assert abs(ind.atr(df, 14).iloc[-1] - 2.0) < 1e-9


def test_macd_bollinger():
    s = pd.Series(np.linspace(1, 2, 100))
    m = ind.macd(s)
    assert m["macd"].iloc[-1] > 0  # uptrend -> fast EMA above slow
    bb = ind.bollinger(pd.Series([10.0] * 30))
    assert bb["upper"].iloc[-1] == bb["lower"].iloc[-1] == 10.0


def test_realized_vol_annualization():
    rng = np.random.default_rng(0)
    r = rng.normal(0, 0.01, 2000)
    idx = pd.date_range("2015-01-01", periods=2001, freq="B", tz="UTC")
    s = pd.Series(100 * np.exp(np.concatenate([[0], np.cumsum(r)])), index=idx)
    rv = ind.realized_vol(s, 250).iloc[-1]
    assert 0.12 < rv < 0.20  # ~0.01*sqrt(~261)


def test_pivots():
    p = ind.classic_pivots(110, 90, 100)
    assert p["P"] == 100 and p["R1"] == 110 and p["S1"] == 90 and p["R2"] == 120 and p["S2"] == 80


def test_trend_regime():
    assert ind.trend_regime(pd.Series(np.linspace(100, 200, 300)))["regime"] == "up"
    assert ind.trend_regime(pd.Series(np.linspace(200, 100, 300)))["regime"] == "down"
    assert ind.trend_regime(pd.Series([1.0, 2.0]))["regime"] == "unknown"


def test_swings_and_zones():
    # sine wave -> repeated swing highs ~ +10 and lows ~ -10 around 100
    x = 100 + 10 * np.sin(np.linspace(0, 8 * np.pi, 400))
    df = frame(x, spread=0.1)
    highs, lows = ind.swing_points(df, 3)
    assert len(highs) >= 3 and len(lows) >= 3
    z = ind.sr_zones(df, atr_value=1.0)
    top_res = max(z["resistance"] + z["support"], key=lambda q: q["touches"])
    assert top_res["touches"] >= 3
    assert any(abs(q["level"] - 110.1) < 0.5 for q in z["resistance"] + z["support"])


def test_cluster_levels():
    cl = ind.cluster_levels(np.array([1.0, 1.1, 1.05, 5.0, 5.2]), tol=0.3)
    assert [c["touches"] for c in cl] == [3, 2]


def test_last_completed_bar():
    df = frame(np.arange(10, dtype=float))
    now_mid = df.index[-1] + pd.Timedelta(hours=5)
    assert ind.last_completed_bar(df, "1d", now_mid).name == df.index[-2]
    now_after = df.index[-1] + pd.Timedelta(days=1, hours=1)
    assert ind.last_completed_bar(df, "1d", now_after).name == df.index[-1]


def test_analyze_frame_runs():
    rng = np.random.default_rng(1)
    df = frame(100 + np.cumsum(rng.normal(0, 1, 400)))
    out = ind.analyze_frame(df, "1d", daily=df, higher=df.resample("W-MON", label="left", closed="left").agg(
        {"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"}))
    for k in ("atr14", "rsi14", "sma200", "pivots", "range_52w", "levels", "trend"):
        assert k in out


def test_weekly_completion():
    idx = pd.date_range("2026-09-14", periods=3, freq="W-MON", tz="UTC")  # Mondays 14, 21, 28
    df = pd.DataFrame({"o": 1.0, "h": 1.0, "l": 1.0, "c": [1.0, 2.0, 3.0], "v": 0.0}, index=idx)
    sat = pd.Timestamp("2026-10-03 12:00", tz="UTC")
    wed = pd.Timestamp("2026-09-30 12:00", tz="UTC")
    assert ind.last_completed_bar(df, "1wk", sat)["c"] == 3.0
    assert ind.last_completed_bar(df, "1wk", wed)["c"] == 2.0


def test_fx_daily_ny_close():
    from app.data import fx_daily_from_hourly
    idx = pd.date_range("2025-07-14 00:00", "2025-07-16 23:00", freq="h", tz="UTC")  # Mon-Wed, EDT
    c = pd.Series(np.arange(len(idx), dtype=float), index=idx)
    h = pd.DataFrame({"o": c, "h": c, "l": c, "c": c, "v": 0.0})
    d = fx_daily_from_hourly(h)
    # 17:00 EDT = 21:00 UTC: Tuesday's bar covers Mon 21:00 UTC .. Tue 20:00 UTC
    tue = d.loc[pd.Timestamp("2025-07-15", tz="UTC")]
    assert tue["o"] == c[pd.Timestamp("2025-07-14 21:00", tz="UTC")]
    assert tue["c"] == c[pd.Timestamp("2025-07-15 20:00", tz="UTC")]
