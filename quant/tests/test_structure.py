import numpy as np
import pandas as pd
import pytest

from app import structure as st
from app.data import OANDA_GRANULARITY, oanda_instrument, parse_oanda_candles
from app.symbols import normalize


def candles_from_closes(closes, start="2025-07-14 00:00", wick=0.1, freq="h"):
    c = np.asarray(closes, float)
    o = np.concatenate([[c[0]], c[:-1]])
    idx = pd.date_range(start, periods=len(c), freq=freq, tz="UTC")
    return pd.DataFrame({"o": o, "h": np.maximum(o, c) + wick, "l": np.minimum(o, c) - wick, "c": c, "v": 0.0},
                        index=idx)


ZIG = [10, 11, 12, 13, 12, 11, 12, 13, 14, 15, 14, 13, 14, 15, 16, 17, 16, 15, 16, 17]


def test_swing_labels_uptrend():
    df = candles_from_closes(ZIG)
    run = st.run_structure(df, 2, 2)
    seq = [(s.kind, s.price, s.label) for s in run.swings]
    assert seq[:6] == [("H", 13, None), ("L", 11, None), ("H", 15, "HH"), ("L", 13, "HL"), ("H", 17, "HH"),
                       ("L", 15, "HL")]
    assert run.trend[-1] == "up"
    # swing H@idx3 (13) confirmed at idx5; first close above 13 is idx8 (14)
    bos = [e for e in run.events if e.type == "bos_up"]
    assert bos[0].idx == 8 and bos[0].level == 13
    # no swing is known before it is confirmed (no look-ahead)
    assert all(s.confirmed_idx == s.idx + 2 for s in run.swings)


def test_downtrend_labels():
    df = candles_from_closes([30 - x for x in ZIG])
    run = st.run_structure(df)
    assert [s.label for s in run.swings[2:6]] == ["LL", "LH", "LL", "LH"]
    assert run.trend[-1] == "down"


def test_sweep_vs_bos():
    df = candles_from_closes([10, 11, 12, 13, 12, 11, 11.5, 12])
    # wick through the 13 body high but close back inside -> sweep, then a body close above -> BOS
    extra = pd.DataFrame({"o": [12.0, 12.5], "h": [13.6, 13.9], "l": [11.9, 12.4], "c": [12.5, 13.7], "v": 0.0},
                         index=pd.date_range(df.index[-1] + pd.Timedelta(hours=1), periods=2, freq="h"))
    df = pd.concat([df, extra])
    run = st.run_structure(df)
    ev = [(e.type, e.idx, e.level) for e in run.events if e.type in ("sweep_high", "bos_up")]
    assert ev == [("sweep_high", 8, 13), ("bos_up", 9, 13)]


def _box_frame(tail_closes):
    # far-away lead-in so no earlier candle can be a mother; mother candle body 10-12
    lead = [5, 5.2, 5.1, 5.3]
    rows = [(5.3, 10), (10, 12)] + []
    df = candles_from_closes(lead)
    o = [10.0, *([12.0] + list(tail_closes[:-1]))]
    c = [12.0, *tail_closes]
    o[0] = 10.0
    extra = pd.DataFrame({"o": o, "c": c}, index=pd.date_range(df.index[-1] + pd.Timedelta(hours=1),
                                                                 periods=len(c), freq="h"))
    extra["h"] = extra[["o", "c"]].max(axis=1) + 0.1
    extra["l"] = extra[["o", "c"]].min(axis=1) - 0.1
    extra["v"] = 0.0
    return pd.concat([df, extra[["o", "h", "l", "c", "v"]]])


def test_box_holding_broken_failed():
    df = _box_frame([11, 11.5, 10.5])
    b = st.box_status(df, len(df) - 1)
    assert b["status"] == "holding" and (b["low"], b["high"]) == (10, 12) and b["n_inside"] == 3
    df2 = _box_frame([11, 11.5, 10.5, 12.8])
    b2 = st.box_status(df2, len(df2) - 1)
    assert b2["status"] == "broken" and b2["direction"] == "up"
    df3 = _box_frame([11, 11.5, 10.5, 12.8, 11.4])
    b3 = st.box_status(df3, len(df3) - 1)
    assert b3["status"] == "failed" and b3["failed_idx"] == len(df3) - 1
    # failed only within 3 candles: 4 closes outside -> stays 'none'/'broken' not failed
    df4 = _box_frame([11, 11.5, 10.5, 12.8, 13.5, 14, 14.5, 11.4])
    assert st.box_status(df4, len(df4) - 1)["status"] != "failed"


def test_impulse_flag():
    df = pd.DataFrame({"o": [10.0, 10.0], "h": [10.5, 12.1], "l": [9.5, 9.95], "c": [10.2, 12.0], "v": 0.0},
                      index=pd.date_range("2025-01-01", periods=2, freq="h", tz="UTC"))
    flags = st.impulse_flags(df, np.array([np.nan, 1.0]))
    assert list(flags) == [False, True]  # body 2.0 >= 1.5 ATR and close in top 25%
    flags2 = st.impulse_flags(df, np.array([np.nan, 1.5]))
    assert not flags2[1]


def test_evaluate_last_and_dedupe_closed_only():
    df = candles_from_closes([10, 11, 12, 13, 12, 11, 12, 12.5, 13.6])
    now_closed = df.index[-1] + pd.Timedelta(minutes=61)
    closed, closes, forming, _ = st.closed_frame(df, "1h", False, now_closed)
    assert forming is None and len(closed) == len(df)
    atr_prev = np.ones(len(closed))
    imp = st.impulse_flags(closed, atr_prev)
    run = st.run_structure(closed)
    sigs = st.evaluate_last(closed, closes, atr_prev, imp, run, "1h")
    assert any(s["type"] == "bos_up" for s in sigs)
    sid = st.signal_id("USDJPY", "1h", "bos_up", closes[-1])
    # a newly forming candle must not change what the last CLOSED candle produced
    nxt = pd.DataFrame({"o": [13.6], "h": [20.0], "l": [13.5], "c": [19.0], "v": 0.0},
                       index=[df.index[-1] + pd.Timedelta(hours=1)])
    df2 = pd.concat([df, nxt])
    closed2, closes2, forming2, _ = st.closed_frame(df2, "1h", False, now_closed + pd.Timedelta(minutes=5))
    assert forming2 is not None and len(closed2) == len(closed)
    assert st.signal_id("USDJPY", "1h", "bos_up", closes2[-1]) == sid


def test_signal_plan_stop_and_targets():
    df = candles_from_closes(ZIG + [16, 15.5, 16.5, 17.5])
    run = st.run_structure(df)
    sig = {"type": "bos_up", "direction": "up", "level": 17.0}
    plan = st.signal_plan(sig, df, run, atr=1.0)
    assert plan["stop"] == pytest.approx(15.5 - 0.2)  # latest HL (body 15.5), confirmed on the last close
    assert any(t["kind"] == "1R" and t["r_multiple"] == 1.0 for t in plan["targets"])


def test_session_tag_dst():
    assert "London" in st.session_tag(pd.Timestamp("2025-07-15 07:30", tz="UTC"))["sessions"]  # BST
    assert "London" not in st.session_tag(pd.Timestamp("2025-01-15 07:30", tz="UTC"))["sessions"]  # GMT
    assert st.session_tag(pd.Timestamp("2025-07-15 07:30", tz="UTC"))["transition"] == "Tokyo→London"
    assert st.session_tag(pd.Timestamp("2025-01-15 13:15", tz="UTC"))["transition"] == "London→New York"


def test_oanda_mapping_and_parse():
    assert OANDA_GRANULARITY["1h"] == "H1" and OANDA_GRANULARITY["4h"] == "H4" and OANDA_GRANULARITY["1d"] == "D"
    assert OANDA_GRANULARITY["1wk"] == "W" and OANDA_GRANULARITY["1mo"] == "M" and OANDA_GRANULARITY["5m"] == "M5"
    assert oanda_instrument(normalize("usdjpy")) == "USD_JPY"
    assert oanda_instrument(normalize("gold")) == "XAU_USD"
    assert oanda_instrument(normalize("spx")) is None
    payload = {"candles": [
        {"complete": True, "volume": 10, "time": "2025-07-14T21:00:00.000000000Z",
         "mid": {"o": "147.1", "h": "147.5", "l": "146.9", "c": "147.2"}},
        {"complete": False, "volume": 3, "time": "2025-07-15T21:00:00.000000000Z",
         "mid": {"o": "147.2", "h": "147.3", "l": "147.0", "c": "147.25"}}]}
    h1 = parse_oanda_candles(payload, "1h")
    assert h1.index[0] == pd.Timestamp("2025-07-14 21:00", tz="UTC") and h1["c"].iloc[0] == 147.2
    d = parse_oanda_candles(payload, "1d")  # D candle starting Mon 17:00 NY = Tuesday trade date
    assert d.index[0] == pd.Timestamp("2025-07-15", tz="UTC")


def test_oanda_disabled_without_token(monkeypatch):
    from app import data
    monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
    assert not data.oanda_enabled()
    monkeypatch.setenv("OANDA_API_TOKEN", "x")
    assert data.oanda_enabled() and data._oanda_cfg()[0].startswith("https://api-fxpractice")


def test_drop_fx_closed():
    from app.data import drop_fx_closed
    idx = pd.DatetimeIndex(["2026-10-02 20:00", "2026-10-02 21:00", "2026-10-03 12:00", "2026-10-04 20:00",
                            "2026-10-04 21:00"], tz="UTC")  # EDT: close Fri 21:00Z, reopen Sun 21:00Z
    df = pd.DataFrame({"o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 0.0}, index=idx)
    kept = drop_fx_closed(df).index
    assert list(kept) == [idx[0], idx[4]]


def test_h4_bins_ny_aligned_across_dst():
    from app.data import resample_ohlc
    for start, first_utc_hours in (("2025-07-14 00:00", {1, 5, 9, 13, 17, 21}), ("2025-01-14 00:00", {2, 6, 10, 14, 18, 22})):
        idx = pd.date_range(start, periods=48, freq="h", tz="UTC")
        df = pd.DataFrame({"o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 0.0}, index=idx)
        out = resample_ohlc(df, "4h")
        assert set(out.index.hour) == first_utc_hours
        metal = resample_ohlc(df, "4h", 2)   # spot metals: grid starts with the 18:00 New York session open
        assert set(metal.index.hour) == {(x + 1) % 24 for x in first_utc_hours}


def test_next_close_skips_weekend():
    sat = pd.Timestamp("2026-10-03 17:00", tz="UTC")
    fri_us = pd.Timestamp("2026-10-02 20:00", tz="UTC")
    assert st.next_close_after(fri_us, "1d", sat, False) == pd.Timestamp("2026-10-05 20:00", tz="UTC")
    fri_fx_h1 = pd.Timestamp("2026-10-02 21:00", tz="UTC")
    assert st.next_close_after(fri_fx_h1, "1h", sat, True) == pd.Timestamp("2026-10-04 22:00", tz="UTC")
