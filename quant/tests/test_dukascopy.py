from datetime import date

import pandas as pd
import pytest

from app import data
from app import dukascopy as dk
from app.symbols import normalize


def test_bi5_roundtrip_synthetic_bytes():
    rows = [(0, 4181.585, 4182.375, 4180.925, 4182.095, 0.025),
            (60, 4182.095, 4182.5, 4181.0, 4181.5, 0.017),
            (120, 4181.5, 4181.5, 4181.5, 4181.5, 0.0),        # no trades -> dropped
            (86340, 4135.0, 4136.0, 4134.5, 4135.815, 0.001)]
    blob = dk.encode_bi5(rows, 1000.0)
    df = dk.decode_bi5(blob, date(2026, 10, 2), 1000.0)
    assert list(df.index) == [pd.Timestamp("2026-10-02 00:00", tz="UTC"), pd.Timestamp("2026-10-02 00:01", tz="UTC"),
                              pd.Timestamp("2026-10-02 23:59", tz="UTC")]
    r = df.iloc[0]
    assert (r["o"], r["h"], r["l"], r["c"]) == pytest.approx((4181.585, 4182.375, 4180.925, 4182.095))
    assert df["c"].iloc[-1] == pytest.approx(4135.815)
    # record layout is big-endian (t, open, close, low, high, float32 volume), 24 bytes
    import lzma, struct
    raw = lzma.decompress(blob)
    assert len(raw) == 4 * 24
    assert struct.unpack(">5If", raw[:24])[:5] == (0, 4181585, 4182095, 4180925, 4182375)
    assert dk.decode_bi5(b"", date(2026, 10, 3), 1000.0).empty
    with pytest.raises(ValueError):
        dk.decode_bi5(lzma.compress(b"x" * 25, format=lzma.FORMAT_ALONE), date(2026, 10, 2), 1000.0)


def test_urls_divisors_instruments():
    assert dk.day_url("XAUUSD", date(2026, 10, 2)).endswith("/XAUUSD/2026/09/02/BID_candles_min_1.bi5")  # month 0-based
    assert dk.day_url("EURUSD", date(2026, 1, 5)).endswith("/EURUSD/2026/00/05/BID_candles_min_1.bi5")
    assert dk.point_divisor("XAUUSD") == 1000 and dk.point_divisor("USDJPY") == 1000
    assert dk.point_divisor("EURUSD") == 100000
    assert dk.instrument(normalize("gold")) == "XAUUSD" and dk.instrument(normalize("eur/usd")) == "EURUSD"
    assert dk.instrument(normalize("SPX")) is None
    days = dk.wanted_days(date(2026, 10, 5), 4)   # Mon back to Thu, Saturday skipped
    assert [d.isoformat() for d in days] == ["2026-10-05", "2026-10-04", "2026-10-02", "2026-10-01"]


@pytest.fixture
def duka_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(dk, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(dk, "MIN_SPACING_S", 0.0)
    monkeypatch.setitem(dk._state, "blocked_until", 0.0)
    monkeypatch.setitem(dk._state, "block_s", dk.BLOCK_BASE_S)
    return tmp_path


def _write(tmp, inst, d, price):
    p = tmp / inst / f"{d.isoformat()}.bi5"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(dk.encode_bi5([(m * 60, price, price + 1, price - 1, price + 0.5, 1.0) for m in range(0, 120)], 1000.0))


def test_load_m1_uses_contiguous_cached_run_and_respects_rate_limit(duka_tmp, monkeypatch):
    for d, px in ((date(2026, 10, 2), 100), (date(2026, 10, 1), 90), (date(2026, 9, 29), 80)):  # 09-30 missing
        _write(duka_tmp, "XAUUSD", d, px)
    calls = []

    class R:
        status_code = 429
        content = b""
    monkeypatch.setattr(dk.httpx, "get", lambda url, timeout=0: calls.append(url) or R())
    m1, cov = dk.load_m1("XAUUSD", date(2026, 10, 2), 5, budget=0)      # default: never fetch inside a request
    assert cov["days_used"] == 2 and calls == []
    m1, cov = dk.load_m1("XAUUSD", date(2026, 10, 2), 5, budget=3)
    assert cov["days_used"] == 2 and cov["from"] == "2026-10-01" and cov["to"] == "2026-10-02" and not cov["complete"]
    assert len(m1) == 240 and m1.index.is_monotonic_increasing
    assert len(calls) == 1 and cov["rate_limited"]                 # one polite attempt, then the breaker opens
    dk.load_m1("XAUUSD", date(2026, 10, 2), 5, budget=3)
    assert len(calls) == 1                                         # no further requests while rate-limited
    assert not dk.start_backfill("XAUUSD", date(2026, 10, 2), 5)


def test_fetch_day_caches_and_404_is_empty_day(duka_tmp, monkeypatch):
    blob = dk.encode_bi5([(0, 1.1, 1.2, 1.0, 1.15, 5.0)], 100000.0)

    class R:
        def __init__(self, code, content=b""):
            self.status_code, self.content = code, content
    seq = iter([R(200, blob), R(404)])
    n = []
    monkeypatch.setattr(dk.httpx, "get", lambda url, timeout=0: n.append(1) or next(seq))
    assert dk.fetch_day("EURUSD", date(2026, 10, 1)) and dk.fetch_day("EURUSD", date(2026, 10, 4))
    assert dk.fetch_day("EURUSD", date(2026, 10, 1)) and len(n) == 2          # second call served from disk
    m1, cov = dk.load_m1("EURUSD", date(2026, 10, 1), 0)
    assert m1["c"].iloc[0] == pytest.approx(1.15) and cov["complete"]


def test_stitch_and_resample():
    idx = pd.date_range("2026-10-01 00:00", periods=40, freq="15min", tz="UTC")
    deep = pd.DataFrame({"o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1.0}, index=idx[:30])
    recent = pd.DataFrame({"o": 106.0, "h": 107.0, "l": 105.0, "c": 106.0, "v": 1.0}, index=idx[10:])
    out, info = data.stitch(deep, recent)
    assert len(out) == 40 and info["tail_candles"] == 10 and info["tail_offset"] == pytest.approx(6.0)
    assert out["c"].iloc[-1] == pytest.approx(100.0) and out["h"].iloc[-1] == pytest.approx(101.0)
    m1 = pd.DataFrame({"o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 1.0},
                      index=pd.date_range("2026-10-01 00:00", periods=60, freq="min", tz="UTC"))
    r = data.resample_m1(m1, "15m")
    assert len(r) == 4 and r["v"].iloc[0] == 15 and r["h"].iloc[0] == 2.0
