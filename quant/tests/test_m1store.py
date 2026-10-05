import io
import zipfile

import numpy as np
import pandas as pd
import pytest

from app import data, m1store as ms
from app.cache import cache
from app.data import replay
from app.symbols import normalize


@pytest.fixture(autouse=True)
def _clean():
    cache.clear()
    yield
    cache.clear()


def histdata_text(start="2026-07-06", days=5, break_at=17, price=100.0, tzname=None):
    """Synthetic HistData file in file-local time: a 1-hour session break starting at `break_at`:00 each day."""
    rows, p = [], price
    for d in pd.date_range(start, periods=days, freq="D"):
        for m in range(24 * 60):
            hh, mm = divmod(m, 60)
            if hh == break_at:
                continue
            p += 0.01
            rows.append(f"{d:%Y%m%d} {hh:02d}{mm:02d}00;{p:.3f};{p + 0.05:.3f};{p - 0.05:.3f};{p + 0.01:.3f};0")
    return "\n".join(rows)


def test_histdata_timezone_is_detected_from_the_session_break():
    ny = ms.parse_histdata_csv(histdata_text(break_at=17))            # summer, break 16:59 -> 18:00 file time
    assert ny.attrs["tz"] == "America/New_York" and "16:5x" in ny.attrs["tz_reason"]
    assert ny.index[0] == pd.Timestamp("2026-07-06 04:00", tz="UTC")  # 00:00 New York (EDT) = 04:00 UTC
    est = ms.parse_histdata_csv(histdata_text(break_at=16))           # break 15:59 -> 17:00: fixed EST file
    assert est.attrs["tz"] == "UTC-5" and est.index[0] == pd.Timestamp("2026-07-06 05:00", tz="UTC")
    winter = ms.parse_histdata_csv(histdata_text(start="2026-01-05", break_at=17))
    assert winter.index[0] == pd.Timestamp("2026-01-05 05:00", tz="UTC")   # EST = New York time in winter
    forced = ms.parse_histdata_csv(histdata_text(break_at=17), tz="UTC-5")
    assert forced.index[0] == pd.Timestamp("2026-07-06 05:00", tz="UTC")
    r = ny.iloc[0]
    assert (r["o"], r["h"], r["l"], r["c"]) == pytest.approx((100.01, 100.06, 99.96, 100.02))


def test_histdata_duplicates_and_bad_rows_are_cleaned():
    txt = ("20260105 000000;1.1;1.2;1.0;1.15;0\n20260105 000100;1.15;1.16;1.14;1.15;0\n"
           "20260105 000000;1.1;1.2;1.0;1.18;0\n20260105 000200;0;0;0;0;0")
    df = ms.parse_histdata_csv(txt, tz="America/New_York")
    assert len(df) == 2 and df["c"].iloc[0] == 1.18 and df.index.is_monotonic_increasing    # last duplicate wins


def test_metatrader_formats_and_time_zones():
    mt5 = ("<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n"
           "2026.07.06\t10:00:00\t4100.1\t4101.0\t4099.5\t4100.6\t120\t0\t25\n"
           "2026.07.06\t10:01:00\t4100.6\t4100.9\t4100.0\t4100.2\t80\t0\t25\n")
    a = ms.parse_metatrader_csv(mt5, tz="Europe/Athens")              # broker server time, UTC+3 in summer
    assert a.index[0] == pd.Timestamp("2026-07-06 07:00", tz="UTC") and a["v"].iloc[0] == 120 and a["c"].iloc[1] == 4100.2
    assert ms.parse_metatrader_csv(mt5, tz="UTC+2").index[0] == pd.Timestamp("2026-07-06 08:00", tz="UTC")
    assert ms.parse_metatrader_csv(mt5).index[0] == pd.Timestamp("2026-07-06 10:00", tz="UTC")
    old = "2026.01.05,09:30,1.1010,1.1015,1.1005,1.1012,55\n2026.01.05,09:31,1.1012,1.1013,1.1009,1.1010,40\n"
    b = ms.parse_metatrader_csv(old, tz="-05:00")
    assert b.index[0] == pd.Timestamp("2026-01-05 14:30", tz="UTC") and b["h"].iloc[0] == 1.1015
    comma = mt5.replace("\t", ",")
    assert ms.parse_metatrader_csv(comma).equals(ms.parse_metatrader_csv(mt5))
    assert ms.symbol_from_filename("DAT_ASCII_EURUSD_M1_2021.csv") == "EURUSD"
    assert ms.symbol_from_filename("XAUUSD_M1_202601010000_202609300000.csv") == "XAUUSD"
    assert ms.symbol_from_filename("123.csv") is None


def make_zip(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in members.items():
            z.writestr(name, content)
    return buf.getvalue()


def test_zip_is_read_in_memory_and_unexpected_members_are_rejected():
    good = make_zip({"DAT_ASCII_EURUSD_M1_2021.csv": "20210104 000000;1;1;1;1;0", "DAT_ASCII_EURUSD_M1_2021.txt": "x"})
    assert ms.read_zip_csv(good).startswith("20210104")
    for bad in ({"a.csv": "x", "run.exe": "x"}, {"../a.csv": "x"}, {"a.csv": "x", "b.csv": "y"}, {"readme.txt": "x"},
                {"sub/a.csv": "x"}):
        with pytest.raises(ValueError):
            ms.read_zip_csv(make_zip(bad))


def test_store_write_merge_coverage_and_gaps():
    idx1 = pd.date_range("2025-12-31 20:00", periods=600, freq="min", tz="UTC")        # spans the year boundary
    df1 = pd.DataFrame({"o": 1.0, "h": 1.1, "l": 0.9, "c": 1.05, "v": 3.0}, index=idx1)
    ms.write_m1("EURUSD", df1, "histdata")
    idx2 = pd.date_range("2026-01-08 00:00", periods=100, freq="min", tz="UTC")        # 7 days later
    ms.write_m1("EURUSD", pd.DataFrame({"o": 2.0, "h": 2.1, "l": 1.9, "c": 2.0, "v": 0.0}, index=idx2), "dukascopy")
    ms.write_m1("EURUSD", df1.iloc[:50].assign(c=9.0), "import:metatrader-file")        # existing minutes are kept
    all_ = ms.load_m1("EURUSD")
    assert len(all_) == 700 and all_.index.is_monotonic_increasing and all_["c"].iloc[0] == 1.05
    ms.write_m1("EURUSD", df1.iloc[:50].assign(c=9.0, h=9.5), "import:metatrader-file", replace_years=True)
    assert ms.load_m1("EURUSD")["c"].iloc[0] == 9.0                                     # the user's file wins
    cov = ms.coverage("EURUSD")[0]
    assert cov["rows"] == 700 and cov["first"] == "2025-12-31T20:00:00Z" and cov["last"] == "2026-01-08T01:39:00Z"
    assert set(cov["sources"]) == {"histdata", "dukascopy", "import:metatrader-file"} and set(cov["years"]) == {"2025", "2026"}
    assert cov["n_gaps"] == 1 and cov["gaps_longer_than_weekend"][0]["from"] == "2026-01-01T05:59:00Z"
    assert ms.has("EURUSD") and not ms.has("GBPUSD") and ms.coverage("GBPUSD") == []


def test_histdata_import_yearly_then_monthly(monkeypatch):
    from datetime import date
    calls = []

    def fake_zip(pair, year, month=None):
        calls.append((year, month))
        if year == 2025 and month is None:
            return make_zip({"DAT_ASCII_EURUSD_M1_2025.csv": histdata_text("2025-07-07", 3)})
        if (year, month) == (2026, 1):
            return make_zip({"DAT_ASCII_EURUSD_M1_202601.csv": histdata_text("2026-01-05", 2)})
        return None                                                    # not published
    monkeypatch.setattr(ms, "histdata_zip", fake_zip)
    rep = ms.import_histdata("EURUSD", 2025, today=date(2026, 3, 15))
    assert calls == [(2025, None), (2026, 1), (2026, 2), (2026, 3)] and rep["missing"] == ["2026-02", "2026-03"]
    assert rep["rows"] == 5 * 23 * 60 and rep["periods"][0]["tz"] == "America/New_York"
    assert ms.coverage("EURUSD")[0]["rows"] == 5 * 23 * 60


def test_histdata_block_stops_the_source(monkeypatch):
    class R:
        def __init__(self, code, text=""):
            self.status_code, self.text = code, text
    monkeypatch.setattr(ms, "HD_SPACING_S", 0.0)
    monkeypatch.setattr(ms.httpx, "get", lambda *a, **k: R(403))
    with pytest.raises(ms.SourceBlocked):
        ms.histdata_zip("EURUSD", 2021)
    monkeypatch.setattr(ms.httpx, "get", lambda *a, **k: R(200, "<html>please solve this CAPTCHA</html>"))
    with pytest.raises(ms.SourceBlocked):
        ms.histdata_zip("EURUSD", 2021)
    monkeypatch.setattr(ms.httpx, "get", lambda *a, **k: R(200, "<html>no form here</html>"))
    assert ms.histdata_zip("EURUSD", 2021) is None                     # nothing published: not a block
    job = ms.start_import(["EURUSD"], 2021, "histdata")
    monkeypatch.setattr(ms.httpx, "get", lambda *a, **k: R(429))
    import time
    for _ in range(100):
        if ms.jobs[job]["status"] != "running":
            break
        time.sleep(0.05)
    assert ms.jobs[job]["status"] in ("done", "failed")


def test_drop_folder_import_with_tz_note(tmp_path):
    ms.IMPORT_DIR.mkdir(parents=True)
    mt5 = ("<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\n"
           + "".join(f"2026.07.06\t10:{m:02d}:00\t4100\t4101\t4099\t4100.5\t10\n" for m in range(30)))
    (ms.IMPORT_DIR / "XAUUSD_M1_pepperstone.csv").write_text(mt5)
    (ms.IMPORT_DIR / "XAUUSD_M1_pepperstone.csv.tz").write_text("Europe/Athens")
    (ms.IMPORT_DIR / "GBPUSD_M1.csv").write_text(mt5.replace("4100.5", "1.305").replace("4100", "1.3")
                                                 .replace("4101", "1.31").replace("4099", "1.29"))
    (ms.IMPORT_DIR / "notes.csv").write_text("hello")
    reps = {r["file"]: r for r in ms.import_folder()}
    x = reps["XAUUSD_M1_pepperstone.csv"]
    assert x["symbol"] == "XAUUSD" and x["rows"] == 30 and x["first"] == "2026-07-06T07:00:00Z" and "tz_warning" not in x
    assert "tz_warning" in reps["GBPUSD_M1.csv"] and reps["GBPUSD_M1.csv"]["first"] == "2026-07-06T10:00:00Z"
    assert "error" in reps["notes.csv"]
    assert ms.coverage("XAUUSD")[0]["sources"] == ["import:metatrader-file"]


def m1_frame(start, periods, base=1.1000):
    idx = pd.date_range(start, periods=periods, freq="min", tz="UTC")
    c = base + 0.00001 * np.arange(periods)
    return pd.DataFrame({"o": c, "h": c + 0.0002, "l": c - 0.0002, "c": c + 0.00001, "v": 1.0}, index=idx)


def test_store_candles_with_live_feed_stitched_on(monkeypatch):
    sym = normalize("EURUSD")
    m1 = m1_frame("2025-06-02 00:00", 3 * 24 * 60)                     # Mon-Wed
    ms.write_m1("EURUSD", m1, "histdata")
    stored = data.resample_m1(m1, "15m")
    # live feed: overlaps the last day (0.0005 higher) and continues 20 candles beyond the store
    idx = pd.date_range("2025-06-04 00:00", periods=96 + 20, freq="15min", tz="UTC")
    lc = np.concatenate([stored["c"].reindex(idx[:96]).to_numpy() + 0.0005, 1.2 + 0.0001 * np.arange(20)])
    live = pd.DataFrame({"o": lc, "h": lc + 0.0003, "l": lc - 0.0003, "c": lc, "v": 0.0}, index=idx.as_unit("ns"))
    monkeypatch.setattr(data, "_live_series", lambda s, iv: data.Series(df=live, source="yahoo/yfinance",
                                                                        ticker="EURUSD=X", interval=iv,
                                                                        delayed_minutes=0))
    s = data.get_series(sym, "15m")
    assert len(s.df) == 3 * 96 + 20 and s.df.index.is_monotonic_increasing and not s.df.index.has_duplicates
    st = s.history["stitch"]
    assert st["tail_candles"] == 20 and st["tail_offset"] == pytest.approx(0.0005) and st["overlap_candles"] == 96
    assert s.df["c"].iloc[3 * 96 - 1] == pytest.approx(stored["c"].iloc[-1])            # store candles untouched
    assert s.df["c"].iloc[-1] == pytest.approx(lc[-1] - 0.0005)                         # tail level-shifted, reported
    assert "local M1 store (histdata" in s.source and "offset vs store" in s.source and s.history["used"]
    assert s.source_info["history"]["store_m1_rows"] == len(m1)
    # other intervals come from the same minutes; 1h bins equal a plain resample
    h1 = data.get_series(sym, "1h").df
    assert h1["h"].iloc[0] == pytest.approx(m1["h"].iloc[:60].max()) and h1.index[0] == m1.index[0]
    # replay over the long history
    with replay("2025-06-03T10:07:00Z"):
        r = data.get_series(sym, "15m")
        assert r.df.index[-1] == pd.Timestamp("2025-06-03 09:45", tz="UTC")
        assert data.get_price(sym)["price"] == pytest.approx(float(data.resample_m1(m1, "5m")["c"][
            pd.Timestamp("2025-06-03 10:00", tz="UTC")]))
    # a symbol without a store is unaffected
    assert data.get_series(normalize("GBPUSD"), "15m").history is None
