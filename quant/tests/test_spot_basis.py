import numpy as np
import pandas as pd
import pytest

from app import data
from app.cache import cache
from app.symbols import normalize


@pytest.fixture(autouse=True)
def _clear():
    cache.clear()
    data._last_good_basis.clear()
    yield
    cache.clear()
    data._last_good_basis.clear()


def fut_series(closes, interval="1h"):
    idx = pd.date_range("2026-10-02 10:00", periods=len(closes), freq="h", tz="UTC")
    c = pd.Series(closes, index=idx, dtype=float)
    df = pd.DataFrame({"o": c, "h": c + 2, "l": c - 2, "c": c, "v": 1.0})
    return data.Series(df=df, source="yahoo/yfinance", ticker="GC=F", interval=interval, delayed_minutes=10)


def mock_sources(monkeypatch, fut_last=4162.3, spot=4140.5, fail_spot=False, fail_gold_api=False):
    monkeypatch.setattr(data, "_raw_series", lambda sym, iv: fut_series([4150.0, 4158.0, fut_last], iv))
    monkeypatch.setattr(data, "_futures_last", lambda sym: {"price": fut_last, "as_of": "2026-10-02T21:00:00Z"})

    def gold_api(code):
        if fail_spot or fail_gold_api:
            raise RuntimeError("down")
        return {"price": spot, "as_of": "2026-10-02T21:00:00Z", "source": "gold-api.com spot"}

    def swiss(code):
        if fail_spot:
            raise RuntimeError("down")
        return {"price": spot + 0.2, "as_of": "2026-10-02T21:00:00Z", "source": "Swissquote public spot quote (mid)"}
    monkeypatch.setattr(data, "_spot_gold_api", gold_api)
    monkeypatch.setattr(data, "_spot_swissquote", swiss)


def test_series_shifted_to_spot(monkeypatch):
    mock_sources(monkeypatch)
    s = data.get_series(normalize("XAUUSD"), "1h")
    assert s.basis["basis"] == pytest.approx(4162.3 - 4140.5)
    assert s.df["c"].iloc[-1] == pytest.approx(4140.5)          # last candle now equals spot
    assert s.df["h"].iloc[0] == pytest.approx(4152.0 - 21.8)    # whole OHLC shifted by one constant
    # returns essentially unchanged; ranges identical
    assert np.allclose((s.df["h"] - s.df["l"]).values, 4.0)
    info = s.source_info
    assert info["basis_adjusted"] and info["basis"] == pytest.approx(21.8) and "caveat" in info["basis_info"]
    assert "basis-adjusted to spot" in info["source"]


def test_price_is_spot(monkeypatch):
    mock_sources(monkeypatch)
    p = data.get_price(normalize("gold"))
    assert p["price"] == pytest.approx(4140.5) and p["spot"] and p["basis_vs_futures"] == pytest.approx(21.8)


def test_swissquote_fallback(monkeypatch):
    mock_sources(monkeypatch, fail_gold_api=True)
    p = data.get_price(normalize("XAUUSD"))
    assert p["price"] == pytest.approx(4140.7) and "Swissquote" in p["source"]


def test_stale_basis_when_spot_down(monkeypatch):
    mock_sources(monkeypatch)
    data.get_basis(normalize("XAUUSD"))  # populate last good
    cache.clear()
    mock_sources(monkeypatch, fail_spot=True)
    b = data.get_basis(normalize("XAUUSD"))
    assert b["stale"] and b["basis"] == pytest.approx(21.8)
    s = data.get_series(normalize("XAUUSD"), "1h")
    assert s.source_info["basis_adjusted"] and s.df["c"].iloc[-1] == pytest.approx(4140.5)


def test_no_basis_serves_futures_unadjusted(monkeypatch):
    mock_sources(monkeypatch, fail_spot=True)
    s = data.get_series(normalize("XAUUSD"), "1h")
    assert not s.source_info["basis_adjusted"] and s.df["c"].iloc[-1] == pytest.approx(4162.3)


def test_non_metal_untouched(monkeypatch):
    mock_sources(monkeypatch)
    s = data.get_series(normalize("SPX"), "1h")
    assert s.basis is None and s.df["c"].iloc[-1] == pytest.approx(4162.3)


def test_parsers():
    assert data.parse_gold_api({"price": 4141.8, "updatedAt": "2026-10-03T18:30:32Z"})["price"] == 4141.8
    sq = [{"topo": {}, "spreadProfilePrices": [{"bid": 4138.9, "ask": 4139.5}], "ts": 1790974800081}]
    q = data.parse_swissquote(sq)
    assert q["price"] == pytest.approx(4139.2) and q["as_of"].startswith("2026-")


def test_basis_curve_math():
    now = pd.Timestamp("2026-10-03 00:00", tz="UTC")
    anchors = [(pd.Timestamp("2026-09-15 12:00", tz="UTC"), 40.0), (pd.Timestamp("2026-10-02 12:00", tz="UTC"), 29.8)]
    curve = data.build_basis_curve(anchors, now, live_basis=20.5)
    assert curve["slope_per_day"] == pytest.approx(-0.6)                      # carry decay per day
    assert curve["basis_now"] == pytest.approx(29.8 - 0.6 * 0.5)              # last anchor carried forward, not 20.5
    b = {"basis": 20.5, "curve": curve, "curve_origin": "2026-10-03T00:00:00Z"}
    idx = pd.DatetimeIndex(["2026-10-03 00:00", "2026-09-15 12:00", "2026-07-01 00:00", "2026-10-02 12:00"], tz="UTC")
    v = data.basis_values(idx, b)
    assert v[0] == pytest.approx(29.5) and v[1] == pytest.approx(40.0) and v[3] == pytest.approx(29.8)
    assert v[2] == pytest.approx(40.0 + 0.6 * 30)                              # carry-extrapolated 30 days, then flat
    old = [(pd.Timestamp("2026-08-01 12:00", tz="UTC"), 50.0), (pd.Timestamp("2026-09-01 12:00", tz="UTC"), 40.0)]
    assert data.build_basis_curve(old, now, 20.5)["basis_now"] == 20.5         # stale anchors -> live quote basis
    assert data.build_basis_curve(anchors[:1], now, 20.5) is None             # one anchor -> constant basis
    assert list(data.basis_values(idx, {"basis": 5.0})) == [5.0] * 4


def test_time_varying_basis_from_cached_spot_days(monkeypatch, tmp_path):
    from datetime import date
    from app import dukascopy as dk
    # futures trade 40 above spot on 09-15 and 30 above on 10-01; live basis now = 29
    hours = pd.date_range("2026-09-14 00:00", "2026-10-02 23:00", freq="h", tz="UTC")
    fut = pd.DataFrame({"o": 4300.0, "h": 4301.0, "l": 4299.0, "c": 4300.0, "v": 1.0}, index=hours)
    monkeypatch.setattr(data, "_raw_series", lambda sym, iv: data.Series(df=fut, source="yahoo/yfinance", ticker="GC=F",
                                                                         interval=iv, delayed_minutes=10))
    monkeypatch.setattr(data, "_futures_last", lambda sym: {"price": 4300.0, "as_of": "2026-10-02T21:00:00Z"})
    monkeypatch.setattr(data, "_spot_gold_api", lambda c: {"price": 4271.0, "as_of": "2026-10-02T21:00:00Z",
                                                           "source": "gold-api.com spot"})
    for d, spot in ((date(2026, 9, 15), 4260.0), (date(2026, 10, 1), 4270.0)):
        p = dk.CACHE_DIR / "XAUUSD" / f"{d.isoformat()}.bi5"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(dk.encode_bi5([(m * 60, spot, spot + 1, spot - 1, spot, 1.0) for m in range(1440)], 1000.0))
    s = data.get_series(normalize("XAUUSD"), "1h")
    info = s.source_info["basis_info"]
    assert info["time_varying"] and [a[0] for a in info["curve"]["anchors"]] == ["2026-09-15", "2026-10-01"]
    c = s.df["c"]
    assert c.iloc[-1] == pytest.approx(4270.0, abs=1.5)                                       # last anchor carried
    # cached days are served from the real spot candles themselves
    assert c[pd.Timestamp("2026-09-15 12:00", tz="UTC")] == 4260.0 and s.df["h"][pd.Timestamp("2026-10-01 12:00", tz="UTC")] == 4271.0
    assert info["spot_overlay"]["days"] == 2 and info["spot_overlay"]["candles_replaced"] == 48
    assert "real spot candles" in s.source
    # a day without spot candles: futures minus the interpolated basis (between 40 and 30)
    assert 4261.0 < c[pd.Timestamp("2026-09-22 12:00", tz="UTC")] < 4269.0
