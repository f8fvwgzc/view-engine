import numpy as np
import pandas as pd
import pytest

from app import data, dataset as D
from app import structure as st
from app.cache import cache
from app.data import replay, resample_ohlc

from .test_retest import walk
from .test_story_replay import SYM, feed, walk15

NF = 250  # features in structure-v3


@pytest.fixture(autouse=True)
def _clean():
    cache.clear()
    yield
    cache.clear()


def contexts(df15, scale=1.0):
    df15 = df15.copy()
    df15[["o", "h", "l", "c"]] *= scale
    out = {"15m": st.context_from_frame(None, "15m", df15, df15.index + pd.Timedelta(minutes=15))}
    end = df15.index[-1] + pd.Timedelta(minutes=15)
    for iv, hrs in (("1h", 1), ("4h", 4)):
        h = resample_ohlc(df15, "1h")
        if iv == "4h":
            h = resample_ohlc(h, "4h")
        h = h[h.index + pd.Timedelta(hours=hrs) <= end]          # closed higher-timeframe candles only
        out[iv] = st.context_from_frame(None, iv, h, h.index + pd.Timedelta(hours=hrs))
    return out


def test_feature_names_are_unique_and_documented():
    names = D.feature_names()
    assert len(names) == len(set(names)) == NF
    assert names[0] == "tr_regime_consolidation" and "ht2_form_pos" in names and names[118] == "align_down"
    assert "d1_swing_trend" in names and "w1_box_pos" in names and "news_mins_to_next" in names
    assert names[161] == "str_diff_1d" and names[-1] == "ht1_cp_pennant_dist_atr"
    assert set(D.feature_docs()) == set(names) and all(len(t) > 10 for t in D.feature_docs().values())
    g = D.feature_groups()
    assert set(g) == set(names) and set(g.values()) == {"session", "news", "structure", "zones", "strength",
                                                        "volatility", "candle", "patterns"}
    assert g["tr_cs_engulfing"] == "patterns" and g["ht1_cp_hs_state"] == "patterns"
    assert g["hour_sin"] == "session" and g["news_next_nfp"] == "news" and g["sup_dist_atr"] == "zones"
    assert g["w1_brk_dir"] == "structure" and g["str_base_4h"] == "strength" and g["sl_atr"] == "volatility"
    assert g["ret16_atr"] == "candle" and D.FEATURE_VERSION == "structure-v3"


def test_labels_known_outcomes():
    #        0     1      2      3      4      5      6
    c = np.array([100.0, 100.2, 101.0, 100.4, 100.1, 100.0, 100.3])
    h = np.array([100.1, 100.6, 101.2, 101.1, 100.5, 100.2, 100.4])
    l = np.array([99.9, 100.0, 100.1, 100.3, 99.4, 99.9, 100.0])
    y = D.labels(h, l, c, pip=0.1, sl_pips=5, tp_pips=10, tp2_pips=30, horizon=3, spread_pips=1)
    # row 0: entry 100. long: +10 pips (101.0) reached on candle 2 (high 101.2) before any low <= 99.5 -> win
    assert y["long_tp1"][0] == 1 and y["short_tp1"][0] == 0 and y["best"][0] == "buy"
    assert y["long_r"][0] == pytest.approx(10 / 5 - 1 / 5) and y["short_r"][0] == pytest.approx(-1 - 1 / 5)
    assert y["long_tp2"][0] == 0                                    # +30 pips never reached in 3 candles
    # row 1: entry 100.2. short stop 100.7 is hit on candle 2 -> loss; long target 101.2 hit on candle 2 -> win
    assert y["long_tp1"][1] == 1 and y["short_tp1"][1] == 0
    # row 2: entry 101.0. short: -10 pips (100.0) reached on candle 4 (low 99.4); stop 101.5 never -> sell
    assert y["short_tp1"][2] == 1 and y["long_tp1"][2] == 0 and y["best"][2] == "sell"
    # row 3: entry 100.4, horizon candles 4,5,6: long stop 99.9 hit on candle 4; short target 99.4 on candle 4 -> sell
    assert y["long_tp1"][3] == 0 and y["short_tp1"][3] == 1
    # rows whose 3-candle horizon is incomplete are null
    assert np.isnan(y["long_tp1"][4:]).all() and list(y["best"][4:]) == [None, None, None]
    # same candle touches stop AND target -> the stop counts
    hh, ll, cc = np.array([10.0, 12.0, 10.0]), np.array([10.0, 8.0, 10.0]), np.array([10.0, 10.0, 10.0])
    y2 = D.labels(hh, ll, cc, pip=0.1, sl_pips=5, tp_pips=10, tp2_pips=15, horizon=1, spread_pips=0)
    assert y2["long_tp1"][0] == 0 and y2["short_tp1"][0] == 0 and y2["long_r"][0] == -1 and y2["best"][0] == "hold"
    # timeout: marked to the close of the last horizon candle, in R
    y3 = D.labels(np.array([10.0, 10.2, 10.3]), np.array([10.0, 9.9, 10.1]), np.array([10.0, 10.1, 10.2]), 0.1, 5, 10,
                  20, 2, 0)
    assert y3["long_tp1"][0] == 0 and y3["long_r"][0] == pytest.approx(0.4) and y3["short_r"][0] == pytest.approx(-0.4)


def test_time_block_sessions_and_dst():
    closes = pd.DatetimeIndex(["2025-07-15 07:15", "2025-01-15 07:15", "2025-07-15 13:00", "2025-07-19 12:00"], tz="UTC")
    blk, _ = D.time_block(closes)
    names = [n for n, _ in D.GLOBAL_FEATURES[:8]]
    row = lambda i: dict(zip(names, blk[i]))                                      # noqa: E731
    assert row(0)["sess_london"] == 1 and row(0)["mins_since_session_open"] == 15     # BST: London opens 07:00 UTC
    assert row(1)["sess_london"] == 0 and row(1)["sess_asia"] == 1                    # GMT: still before 08:00
    assert row(2)["sess_overlap"] == 1 and row(2)["sess_newyork"] == 1 and row(2)["mins_since_session_open"] == 60
    assert row(3)["sess_london"] == 0 and np.isnan(row(3)["mins_since_session_open"])  # Saturday
    assert row(0)["dow"] == 1 and row(0)["hour_sin"] == pytest.approx(np.sin(2 * np.pi * 7.25 / 24))


def test_forming_candle_block_and_grids():
    idx = pd.date_range("2025-07-15 10:00", periods=8, freq="15min", tz="UTC")
    df = pd.DataFrame({"o": [10, 11, 12, 13, 13.0, 13.4, 13.2, 12.7], "h": [11, 12, 13, 13.5, 13.6, 14.2, 13.3, 12.9],
                       "l": [9.9, 10.9, 11.9, 12.9, 12.8, 13.1, 12.6, 12.5],
                       "c": [11, 12, 13, 13.0, 13.4, 13.2, 12.7, 12.8], "v": 1.0}, index=idx)
    blk, raw = D.forming_block(df, idx + pd.Timedelta(minutes=15), np.ones(8), "1h", 1)
    # row 5 = second candle of the 11:00 hour: open 13.0, high so far 14.2, low 12.8, close 13.2
    assert blk[5] == pytest.approx([0.2, 0.5, (13.2 - 12.8) / (14.2 - 12.8)])
    assert blk[3][1] == 1.0 and blk[4][1] == 0.25 and raw["open"][7] == 13.0
    # H4 grid: FX starts 17:00 New York (21:00 UTC in summer), spot metals 18:00 New York
    t = pd.DatetimeIndex(["2025-07-15 13:30"], tz="UTC")
    assert D.bin_starts(t, "4h", 1)[0][0] == pd.Timestamp("2025-07-15 13:00", tz="UTC")
    assert D.bin_starts(t, "4h", 2)[0][0] == pd.Timestamp("2025-07-15 10:00", tz="UTC")
    assert D.bin_starts(t, "1d")[0][0] == pd.Timestamp("2025-07-14 21:00", tz="UTC")


def test_no_lookahead_truncation_leaves_rows_identical():
    df = walk(4000, seed=17)
    T = 2700
    full = D.build_matrix(contexts(df), "15m", pip=0.1, sl_pips=20, tp_pips=50, horizon=48, spread_pips=2.5)
    cut = D.build_matrix(contexts(df.iloc[:T + 1]), "15m", pip=0.1, sl_pips=20, tp_pips=50, horizon=48, spread_pips=2.5)
    pert = df.copy()
    pert.iloc[T + 1:, :4] += 300.0                                   # a different future
    moved = D.build_matrix(contexts(pert), "15m", pip=0.1, sl_pips=20, tp_pips=50, horizon=48, spread_pips=2.5)
    a, b, c = full["X"][:T + 1], cut["X"], moved["X"][:T + 1]
    assert b.shape == a.shape == (T + 1, NF)
    np.testing.assert_array_equal(np.isnan(a), np.isnan(b))
    np.testing.assert_allclose(np.nan_to_num(a), np.nan_to_num(b), rtol=0, atol=1e-9)
    np.testing.assert_allclose(np.nan_to_num(a), np.nan_to_num(c), rtol=0, atol=1e-9)
    # the labels (the only thing allowed to look ahead) are null where the horizon is incomplete
    assert np.isnan(cut["y"]["long_tp1"][-48:]).all() and not np.isnan(cut["y"]["long_tp1"][-49])
    # the matrix is informative: every feature block has finite values after the warm-up
    X = full["X"][D.WARMUP:, :119]                                 # (news / strength / d1-w1 are null here)
    assert np.isfinite(X).mean() > 0.85 and (np.nanstd(X, axis=0) > 0).mean() > 0.9
    # pattern labels look ahead by design, but only where their outcome is known
    for k, v in cut["pattern_labels"].items():
        assert np.isnan(v[-3:]).all() or k == "fakeout", k
    assert set(cut["pattern_labels"]) == set(D.PATTERN_LABELS)


def test_features_are_scale_free():
    df = walk(2500, seed=4)
    a = D.build_matrix(contexts(df), "15m", pip=0.1, sl_pips=20, tp_pips=50)
    b = D.build_matrix(contexts(df, scale=100.0), "15m", pip=10.0, sl_pips=20, tp_pips=50)   # e.g. another symbol
    np.testing.assert_array_equal(np.isnan(a["X"]), np.isnan(b["X"]))
    np.testing.assert_allclose(np.nan_to_num(a["X"]), np.nan_to_num(b["X"]), rtol=1e-6, atol=1e-6)
    for k in ("long_tp1", "short_tp1", "long_r"):
        np.testing.assert_allclose(np.nan_to_num(a["y"][k]), np.nan_to_num(b["y"][k]), atol=1e-6)


def test_structure_features_on_known_series():
    from .test_mtf import BASE, from_closes
    h1 = from_closes(BASE + [185.5])
    ctx = st.context_from_frame(None, "1h", h1, h1.index + pd.Timedelta(hours=1))
    s = D.tf_state(ctx)
    names = [n for n, _ in D.TF_FEATURES]
    blk = D.tf_block(s, np.arange(len(h1)), h1["c"].to_numpy(float), ctx.atr, True)
    last = dict(zip(names, blk[-1]))
    assert last["regime_consolidation"] == 1 and last["box_exists"] == 1 and last["box_inside"] == 1
    assert last["box_pos"] == pytest.approx((185.5 - 183.0) / 3.5) and last["box_age"] > 5
    # the most recent impulse leg is the first bounce off the low (181 -> 187), long finished by now
    assert last["imp_dir"] == 1 and last["imp_size_atr"] > 2.5 and last["imp_age"] > 8
    assert last["dist_swing_high_atr"] > 0 and last["dist_swing_low_atr"] > 0
    drop = dict(zip(names, blk[24]))                                  # during the impulse (index 24 = its low)
    assert drop["regime_impulse_down"] == 1 and drop["imp_age"] == 0 and drop["box_exists"] == 0


def test_service_features_equal_dataset_row_and_replay(monkeypatch):
    full = walk15()
    monkeypatch.setattr(data, "_live_series", feed(full))
    T = pd.Timestamp("2025-06-24 14:20", tz="UTC")
    with replay(T):
        f = D.features(SYM, "15m", pip=0.01)
        d = D.dataset(SYM, "15m", pip=0.01, max_rows=500)
    assert f["time"] == d["times"][-1] == "2025-06-24T14:15:00Z" and d["n"] == 500 == len(d["X"])
    assert f["x"] == d["X"][-1] and f["feature_names"] == d["feature_names"] and len(f["x"]) == NF
    assert d["y"]["long_tp1"][-1] is None and d["y"]["best"][-1] is None and d["y"]["best"][0] in ("buy", "sell", "hold")
    assert set(d["y"]) == {"long_tp1", "short_tp1", "long_tp2", "short_tp2", "long_r", "short_r", "best"}
    assert set(d["params"]) == {"sl_pips", "tp_pips", "tp2_pips", "horizon", "spread_pips"}
    assert f["facts"]["timeframes"]["1h"]["regime"] in D._REG and "forming" in f["facts"] and "zones" in f["facts"]
    assert "17:00 New York" in f["data_note"]["h4_grid"]
    # the same moment taken from the un-truncated export is identical: later candles change nothing
    cache.clear()
    live = D.dataset(SYM, "15m", pip=0.01, max_rows=100000)
    i = live["times"].index("2025-06-24T14:15:00Z")
    assert live["X"][i] == f["x"] and live["y"]["long_tp1"][i] is not None
    with pytest.raises(ValueError):
        D.dataset(SYM, "1d")


def test_http_dataset_and_features(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main
    monkeypatch.setattr(data, "_live_series", feed(walk15()))
    monkeypatch.setattr(main, "normalize", lambda s: SYM)
    c = TestClient(main.app)
    r = c.get("/dataset", params={"symbol": "x", "interval": "15m", "pip": 0.01, "max_rows": 50,
                                  "as_of": "2025-06-24T14:20:00Z"})
    d = r.json()
    assert r.status_code == 200 and d["n"] == 50 and d["replay"] is True and d["feature_version"] == D.FEATURE_VERSION
    assert len(d["X"][0]) == len(d["feature_names"]) and len(d["times"]) == len(d["y"]["long_r"]) == 50
    f = c.get("/features", params={"symbol": "x", "interval": "15m", "pip": 0.01, "as_of": "2025-06-24T14:20:00Z"}).json()
    assert f["x"] == d["X"][-1] and f["replay"] is True and f["price"] > 0
    assert c.get("/dataset", params={"symbol": "x", "interval": "1d"}).status_code == 400


def j_pl_ok(z):
    """Pattern labels are 0/1/NaN and at least the pullback one occurs."""
    for k in D.PATTERN_LABELS:
        v = z[k]
        if not np.isin(v[~np.isnan(v)], (0.0, 1.0)).all():
            return False
    return bool((~np.isnan(z["cont_after_pullback"])).sum() > 20)


def test_npz_export_round_trip_range_and_disk_cache(monkeypatch):
    import json
    monkeypatch.setattr(data, "_live_series", feed(walk15()))
    p = D.dataset_npz(SYM, "15m", pip=0.01, start="2025-06-10T00:00:00Z", end="2025-06-20T00:00:00Z")
    z = np.load(p, allow_pickle=False)
    assert set(z.files) == {"X", "times", "long_tp1", "short_tp1", "long_tp2", "short_tp2", "long_r", "short_r",
                            "best", "meta", *D.PATTERN_LABELS}
    assert z["fakeout"].dtype == np.float32 and len(z["cont_after_pullback"]) == len(z["times"])
    assert z["X"].dtype == np.float32 and z["times"].dtype == np.int64 and z["best"].dtype == np.int8
    assert z["long_r"].dtype == np.float32 and z["X"].shape == (len(z["times"]), NF)
    meta = json.loads(str(z["meta"]))
    assert meta["n"] == len(z["times"]) and meta["feature_names"] == D.feature_names() and meta["symbol"] == "TESTFX"
    assert meta["label_names"] == list(D.PATTERN_LABELS) and set(meta["label_docs"]) >= set(D.PATTERN_LABELS)
    assert meta["feature_groups"] == D.feature_groups() and meta["feature_version"] == "structure-v3"
    assert j_pl_ok(z)
    assert meta["start"] >= "2025-06-10" and meta["end"] <= "2025-06-20T00:00:00Z" and meta["build_seconds"] >= 0
    t = pd.to_datetime(z["times"], unit="s", utc=True)
    assert t[0] >= pd.Timestamp("2025-06-10", tz="UTC") and t[-1] <= pd.Timestamp("2025-06-20", tz="UTC")
    # identical to the JSON export of the same range
    j = D.dataset(SYM, "15m", pip=0.01, start="2025-06-10T00:00:00Z", end="2025-06-20T00:00:00Z", max_rows=100000)
    assert j["n"] == meta["n"] and j["times"][0] == meta["start"]
    np.testing.assert_allclose(np.nan_to_num(np.array(j["X"], dtype=float)), np.nan_to_num(z["X"].astype(float)),
                               atol=1e-5)
    code = {"hold": 0, "buy": 1, "sell": 2, None: -1}
    assert [code[b] for b in j["y"]["best"]] == z["best"].tolist()
    assert np.array_equal(np.isnan(z["long_tp1"]), z["best"] == -1)
    # finished exports are cached on disk
    mt = p.stat().st_mtime_ns
    assert D.dataset_npz(SYM, "15m", pip=0.01, start="2025-06-10T00:00:00Z", end="2025-06-20T00:00:00Z") == p
    assert p.stat().st_mtime_ns == mt and p.parent == D.EXPORT_DIR
    assert D.dataset_npz(SYM, "15m", pip=0.01, sl_pips=30) != p


def test_chunked_computation_keeps_features_equal_and_causal(monkeypatch):
    monkeypatch.setattr(D, "CHUNK", 700)
    monkeypatch.setattr(D, "CHUNK_WARM", 400)
    full = walk15()
    monkeypatch.setattr(data, "_live_series", feed(full))
    T = pd.Timestamp("2025-06-24 14:20", tz="UTC")
    live = D.dataset(SYM, "15m", pip=0.01, max_rows=100000)
    assert live["n"] > 1500                                             # several chunks of 700
    X = np.array(live["X"], dtype=float)[:, :119]
    assert np.isfinite(X).mean() > 0.85
    with replay(T):
        f = D.features(SYM, "15m", pip=0.01)
        d = D.dataset(SYM, "15m", pip=0.01, max_rows=900)               # spans a chunk boundary
    assert f["x"] == d["X"][-1]
    i = live["times"].index(f["time"])
    assert live["X"][i] == f["x"]                                       # the future does not change the row
    j = live["times"].index(d["times"][0])
    assert live["X"][j:i + 1] == d["X"]


def fake_events():
    rows = [("2025-06-24T12:30:00Z", "USD", "nfp", True), ("2025-06-24T18:00:00Z", "USD", "fomc", True),
            ("2025-06-24T15:00:00Z", "USD", "fomc", False),           # unscheduled: never a feature
            ("2025-06-20T12:30:00Z", "USD", "claims", True), ("2025-06-24T13:00:00Z", "EUR", "central_bank", True)]
    return {"built": "test", "events": [{"time_utc": t, "currency": c, "type": ty, "title": ty, "source": "test",
                                         "approximate": False, "scheduled": s} for t, c, ty, s in rows]}


def test_news_block_uses_only_the_schedule(monkeypatch):
    from app import events as EV
    monkeypatch.setattr(EV, "load_table", fake_events)
    t = pd.DatetimeIndex(["2025-06-24 12:00", "2025-06-24 12:30", "2025-06-24 14:20", "2025-06-22 00:00"], tz="UTC")
    b = EV.news_block(t, ("USD",))
    row = lambda i: dict(zip(EV.NEWS_FEATURES, b[i]))                              # noqa: E731
    assert row(0)["news_mins_to_next"] == 30 and row(0)["news_next_nfp"] == 1 and row(0)["news_within_60m"] == 1
    assert row(0)["news_mins_since_last"] == 1440 and row(0)["news_last_claims"] == 0   # last one is > 24h ago
    assert row(1)["news_mins_since_last"] == 0 and row(1)["news_last_nfp"] == 1         # released at this close
    assert row(1)["news_mins_to_next"] == 330 and row(1)["news_next_fomc"] == 1         # the 15:00 one is skipped
    assert row(2)["news_mins_since_last"] == 110 and row(2)["news_within_60m"] == 0 and row(2)["news_today"] == 1
    assert row(3)["news_today"] == 0 and row(3)["news_mins_to_next"] == 1440 and row(3)["news_next_nfp"] == 0
    both = EV.news_block(t[:1], ("EUR", "USD"))
    assert both[0][0] == 30
    assert np.isnan(EV.news_block(t, ("CHF",))).all()


def test_service_news_and_context_features_are_causal(monkeypatch):
    from app import events as EV
    monkeypatch.setattr(EV, "load_table", fake_events)
    full = walk15()
    monkeypatch.setattr(data, "_live_series", feed(full))
    with replay("2025-06-24T14:20:00Z"):
        f = D.features(SYM, "15m", pip=0.01)
    v = dict(zip(f["feature_names"], f["x"]))
    assert v["news_mins_since_last"] == 105 and v["news_last_nfp"] == 1 and v["news_mins_to_next"] == 225
    assert f["facts"]["news"]["next_scheduled"][0]["type"] == "fomc" and f["facts"]["news"]["today"] is True
    assert v["d1_swing_trend"] is not None and v["d1_brk_dir"] is not None        # daily context from closed days
    assert v["str_base_1h"] is None                                                # not a major: no strength
    cache.clear()
    live = D.dataset(SYM, "15m", pip=0.01, max_rows=100000)
    i = live["times"].index(f["time"])
    assert live["X"][i] == f["x"]


def test_pattern_outcome_labels_on_known_series():
    from .test_mtf import BASE, from_closes
    # range 183-187 after the impulse; then a body close below 183 that comes back inside (fakeout) ...
    closes = BASE + [182.0, 182.4, 184.5, 185.5, 186.0, 185.0, 184.0, 185.0, 186.0, 185.5, 185.0, 185.5]
    h1 = from_closes(closes)
    ctx = st.context_from_frame(None, "1h", h1, h1.index + pd.Timedelta(hours=1))
    res = D.build_matrix({"1h": ctx}, "1h", pip=0.1, sl_pips=5, tp_pips=10, horizon=6)
    pl = res["pattern_labels"]
    n0 = len(BASE)
    assert pl["fakeout"][n0] == 1 and np.isnan(pl["fakeout"][n0 - 1]) and np.nansum(~np.isnan(pl["fakeout"])) >= 1
    # rows right after that break: price closed back inside before any retest-and-go -> 0
    assert pl["retest_then_continue"][n0] == 0 and np.isnan(pl["retest_then_continue"][n0 + 5])
    # ... and a real break that is retested and runs
    closes2 = BASE + [188.8, 189.5, 187.3, 188.5, 189.9, 190.5, 191.0, 191.5, 192.0, 192.5, 193.0]
    h2 = from_closes(closes2, wicks={n0 + 2: {"l": 186.4}})
    ctx2 = st.context_from_frame(None, "1h", h2, h2.index + pd.Timedelta(hours=1))
    pl2 = D.build_matrix({"1h": ctx2}, "1h", pip=0.1, sl_pips=5, tp_pips=10, horizon=6)["pattern_labels"]
    assert pl2["fakeout"][n0] == 0 and pl2["retest_then_continue"][n0] == 1 and pl2["retest_then_continue"][n0 + 1] == 1
    assert np.isnan(pl2["retest_then_continue"][-1])                         # outcome not known yet
    # continuation after a pullback: uptrend zigzag, pullback rows are labelled by what closes first
    zig = [10, 11, 12, 13, 12, 11, 12, 13, 14, 15, 14, 13, 14, 15, 16, 17, 16, 15, 16, 17, 18, 19, 18, 17, 18, 19, 20]
    hz = from_closes([100 + 2 * x for x in zig], wick=0.3)
    cz = st.context_from_frame(None, "1h", hz, hz.index + pd.Timedelta(hours=1))
    rz = D.build_matrix({"1h": cz}, "1h", pip=0.1, sl_pips=5, tp_pips=10, horizon=5)
    names = rz["names"]
    pd_ = rz["X"][:, names.index("tr_pullback_dir")]
    lab = rz["pattern_labels"]["cont_after_pullback"]
    rows = np.flatnonzero((pd_ == 1) & ~np.isnan(lab))
    assert len(rows) >= 2 and (lab[rows] == 1).all()                         # every pullback made a new high
    assert np.isnan(lab[pd_ == 0]).all()


def test_4h_trigger_with_daily_and_weekly(monkeypatch):
    full = walk15(n=40000, seed=8)                                  # ~14 months of 15m candles
    monkeypatch.setattr(data, "_live_series", feed(full))
    d = D.dataset(SYM, "4h", pip=0.01, max_rows=400)
    assert d["timeframes"] == {"tr": "4h", "ht1": "1d", "ht2": "1wk", "d1": "1d", "w1": "1wk"} and d["n"] == 400
    v = dict(zip(d["feature_names"], d["X"][-1]))
    assert v["ht1_swing_trend"] == v["d1_swing_trend"] and v["ht2_box_pos"] == v["w1_box_pos"]
    assert v["w1_swing_trend"] is not None and 0 < v["ht2_form_elapsed"] <= 1
    f = D.features(SYM, "4h", pip=0.01)
    assert f["x"] == d["X"][-1] and f["timeframes"]["ht2"] == "1wk"


def test_1m_trigger_alias_and_news_coverage(monkeypatch):
    from app import events as EV
    from app import main
    monkeypatch.setattr(EV, "load_table", fake_events)
    assert main._trigger_interval("1m") == "1min" and main._trigger_interval("15m") == "15m"
    assert D.news_coverage(SYM) == {"USD": ["claims", "fomc", "nfp"]}
    from app.symbols import normalize
    cov = D.news_coverage(normalize("GBPJPY"))
    assert cov == {"GBP": [], "JPY": []}                            # nothing for these in the fake table


def test_features_applicable_flags_and_docs(monkeypatch):
    full = walk15()
    monkeypatch.setattr(data, "_live_series", feed(full))
    seen = {k: set() for k in D.PATTERN_LABELS}
    for hh in range(0, 40):
        T = pd.Timestamp("2025-06-23 08:20", tz="UTC") + pd.Timedelta(minutes=15 * hh)
        cache.clear()
        with replay(T):
            f = D.features(SYM, "15m", pip=0.01)
        v = dict(zip(f["feature_names"], f["x"]))
        a = f["applicable"]
        assert set(a) == set(D.PATTERN_LABELS) and all(isinstance(x, bool) for x in a.values())
        assert a["cont_after_pullback"] == (v["tr_pullback_dir"] != 0)
        assert a["ht1_cont_after_pullback"] == (v["ht1_pullback_dir"] != 0)
        assert a["fakeout"] == (v["tr_brk_age"] == 0 and v["tr_brk_dir"] != 0)
        if a["retest_then_continue"]:
            assert v["tr_brk_age"] <= 3 and v["tr_box_inside"] == 0
        for k, x in a.items():
            seen[k].add(x)
    assert seen["cont_after_pullback"] == {True, False} and True in seen["retest_then_continue"]
    assert f["feature_docs"] == D.feature_docs() and set(f["label_docs"]) == set(D.PATTERN_LABELS)
    assert "patterns" in f["facts"] and set(f["facts"]["patterns"]) == {"15m", "1h"}


def test_applicable_mask_matches_the_labels():
    df = walk(3000, seed=21)
    res = D.build_matrix(contexts(df), "15m", pip=0.1, sl_pips=20, tp_pips=50, horizon=48)
    for k in D.PATTERN_LABELS:
        lab, app = res["pattern_labels"][k], res["applicable"][k]
        assert not (~np.isnan(lab) & ~app).any(), k                  # a labelled row is always an applicable row
        body = slice(300, len(lab) - 400)                           # away from the ends the two coincide
        assert (np.isnan(lab[body]) == ~app[body]).all(), k
        assert app.sum() > 20, k


def test_pattern_features_fire_on_random_data_and_are_causal():
    df = walk(4000, seed=17)
    res = D.build_matrix(contexts(df), "15m", pip=0.1)
    X, names = res["X"], res["names"]
    col = lambda n: X[300:, names.index(n)]                          # noqa: E731
    for n_ in ("tr_cs_engulfing", "tr_cs_inside", "tr_cs_outside", "ht1_cs_engulfing"):
        assert (col(n_) != 0).sum() > 10, n_
    assert set(np.unique(col("tr_cs_engulfing"))) <= {-1.0, 0.0, 1.0}
    assert sum((col(f"tr_cp_{f}_active") == 1).sum() > 0 for f, _ in __import__("app.patterns", fromlist=["x"]).FAMILIES) >= 4
    act = col("tr_cp_double_active") == 1
    assert np.isnan(col("tr_cp_double_dist_atr")[~act]).all() and np.isfinite(col("tr_cp_double_dist_atr")[act]).all()
    assert set(np.unique(col("tr_cp_double_state"))) <= {0.0, 1.0, 2.0}
