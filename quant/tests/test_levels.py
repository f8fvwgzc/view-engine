import numpy as np
import pandas as pd
import pytest

from app import data, levels as lv
from app import structure as st
from app.cache import cache
from app.data import replay, resample_ohlc
from app.retest import Level

from .test_retest import walk
from .test_story_replay import SYM, feed, walk15

T0 = pd.Timestamp("2025-07-14 00:00", tz="UTC")


def frame(rows):
    a = np.array(rows, float)
    idx = pd.date_range(T0, periods=len(a), freq="15min", tz="UTC")
    return a[:, 0], a[:, 1], a[:, 2], a[:, 3], data.ns_index(idx + pd.Timedelta(minutes=15))


def zone(price=100.0, tol=0.2, known=T0, members=None):
    members = members or [{"interval": "15m", "price": price, "label": "HL", "kind": "L", "time": known,
                           "known_time": known, "known_pos": 0.0, "expires_pos": 1e9}]
    return {"id": 0, "price": price, "tol": tol, "members": members, "known_time": known, "first_seen": known,
            "expires_pos": 1e9}


SEQ = [  # o, h, l, c   (zone 99.8–100.2, ATR 1, pip 0.1)
    (102.0, 102.2, 101.8, 102.0), (102.0, 102.1, 101.4, 101.5),
    (101.5, 101.6, 100.1, 100.8),      # 2 touches, closes back above
    (100.8, 101.3, 100.7, 101.2),      # 3 moves >= 1 ATR away        -> rejection
    (101.2, 101.6, 101.1, 101.5),
    (101.5, 101.5, 99.5, 100.6),       # 5 wick 5 pips through, body closes back
    (100.6, 101.2, 100.5, 101.1),      # 6 1 ATR away                 -> sweep
    (101.1, 101.3, 100.9, 101.0),
    (101.0, 101.0, 98.9, 99.0),        # 8 body CLOSE below the zone  -> break
    (99.0, 99.1, 98.4, 98.5),
    (98.5, 99.9, 98.4, 99.3),          # 10 back up into the zone from below, closes below
    (99.3, 99.4, 98.9, 99.0),          # 11 1 ATR away downward       -> retest_hold (support became resistance)
    (99.0, 99.2, 98.7, 98.8), (98.8, 99.0, 98.5, 98.6)]


def react(rows, z=None):
    o, h, l, c, closes = frame(rows)
    z = z or zone()
    r = lv.zone_reactions(z, o, h, l, c, closes, np.ones(len(c)), np.zeros(len(c), bool), 0.1)
    return z, r


def test_rejection_sweep_break_retest_hold_sequence():
    z, r = react(SEQ)
    assert [(x["type"], x["idx"], x["resolved_idx"]) for x in r] == [
        ("rejection", 2, 3), ("sweep", 5, 6), ("break", 8, 8), ("retest_hold", 10, 11)]
    rej, swp, brk, rth = r
    assert rej["role_tested"] == "support" and rej["wick_depth_pips"] == 0 and not rej["swept"]
    assert swp["wick_depth_pips"] == pytest.approx(5.0) and swp["swept"]
    assert rth["role_tested"] == "resistance" and rth["is_retest"]
    # features are frozen at the first touch: what had already happened by then
    assert [x["prior_holds"] for x in r] == [0, 1, 2, 2] and [x["flipped"] for x in r] == [False] * 4
    assert rej["follow_through_pips"] == pytest.approx((101.6 - 100) / 0.1)          # best high in the next candles
    assert brk["follow_through_pips"] == pytest.approx((100 - 98.4) / 0.1)           # continuation after the break
    assert (z["holds"], z["flips"], z["closes_through"], z["pending_retest"]) == (3, 1, 1, False)


def test_retest_fail_and_in_progress():
    rows = SEQ[:10] + [(98.5, 100.6, 98.4, 100.5), (100.5, 100.9, 100.4, 100.8)]     # closes back through
    z, r = react(rows)
    assert [x["type"] for x in r][-1] == "retest_fail" and z["closes_through"] == 2 and z["pending_retest"]
    z, r = react(SEQ[:3])                                                           # touch, not yet decided
    assert [x["type"] for x in r] == ["in_progress"] and z["holds"] == 0
    assert lv.classify_last_candle(r[-1], z, SEQ[2], 2) == "touch_holding_provisional"
    z, r = react(SEQ[:6])
    assert lv.classify_last_candle(r[-1], z, SEQ[5], 5) == "sweep_provisional"
    z, r = react(SEQ[:9])
    assert lv.classify_last_candle(r[-1], z, SEQ[8], 8) == "break"
    z, r = react(SEQ[:5])
    assert lv.classify_last_candle(r[-1], z, SEQ[4], 4) is None                     # candle 4 did not touch


def test_zone_unknown_before_confirmation_and_stall():
    z, r = react(SEQ, zone(known=T0 + pd.Timedelta(minutes=15 * 7)))                # known only after candle 6
    assert [x["idx"] for x in r] == [8, 10] and r[0]["prior_holds"] == 0            # earlier circles do not exist
    flat = [(100.5, 100.6, 100.4, 100.5)] + [(100.5, 100.6, 100.1, 100.4)] * 20     # sits on the zone, goes nowhere
    z, r = react(flat)
    assert r[0]["type"] == "stall" and z["holds"] == 0 and z["closes_through"] == 0


def test_zone_merging_and_confluence_is_causal():
    closes = data.ns_index(pd.date_range(T0, periods=3000, freq="15min", tz="UTC") + pd.Timedelta(minutes=15))
    atr = np.ones(3000)                                                             # tol = 0.25
    t = lambda h: T0 + pd.Timedelta(hours=h)                                        # noqa: E731
    lvls = [Level(100.0, "L", "HL", t(1), t(2), "15m"), Level(100.2, "H", "LH", t(3), t(5), "1h"),
            Level(101.0, "H", "HH", t(4), t(6), "15m"), Level(100.3, "L", "LL", t(7), t(8), "15m"),
            Level(100.1, "L", "HL", t(400), t(401), "15m")]
    zs = lv.build_zones(lvls, closes, atr, "15m")
    assert [round(z["price"], 2) for z in zs] == [100.0, 101.0, 100.3, 100.1]
    z0 = zs[0]
    assert [m["interval"] for m in z0["members"]] == ["15m", "1h"]                  # 100.2 merged (within 0.25)
    assert lv.confluence_at(z0, t(4)) == ["15m"] and lv.confluence_at(z0, t(6)) == ["1h", "15m"]
    # lifetime in base candles: the 1h member (known at candle 20) keeps it alive 200 x 4 more 15m candles
    assert z0["expires_pos"] == pytest.approx(20 + 800) and zs[1]["expires_pos"] == pytest.approx(24 + 200)
    assert lv.base_position(closes, T0 - pd.Timedelta(hours=10)) == pytest.approx(-41.0, abs=0.1)
    # 100.3 is 0.3 away from the anchor -> its own zone; 100.1 arrives after zone 0 expired -> new zone
    assert zs[3]["known_time"] == t(401)
    assert lv.confluence_bucket(["1h", "15m"], ["4h", "1h", "15m"]) == "+1h"
    assert lv.confluence_bucket(["15m"], ["4h", "1h", "15m"]) == "15m only"
    assert lv.confluence_bucket(["4h", "15m"], ["4h", "1h", "15m"]) == "+4h"
    assert [lv.touches_bucket(n) for n in (0, 2, 3, 7)] == ["0", "2", "3+", "3+"]


def test_bucket_stats_wilson_and_reliability():
    eps = [{"type": "rejection", "follow_through_pips": 20.0}] * 40 + [{"type": "break", "follow_through_pips": 8.0}] * 10
    b = lv.bucket_stats(eps, overall_rate=0.6)
    assert b["n"] == 50 and b["p_hold"] == 0.8 and b["ci95_hold"][0] == pytest.approx(0.669, abs=2e-3)
    assert b["ci_clear_of_50"] and b["differs_from_overall"] and not b["unreliable"]
    assert b["median_follow_through_after_hold_pips"] == 20 and b["median_follow_through_after_break_pips"] == 8
    small = lv.bucket_stats(eps[:5] + eps[-5:])
    assert small["unreliable"] and not small["ci_clear_of_50"]
    coin = lv.bucket_stats(eps[:25] + eps[-10:] * 2 + eps[-5:])
    assert not coin["ci_clear_of_50"]


def test_forming_candle_from_lower_timeframe():
    idx = pd.date_range("2025-07-15 09:00", periods=7, freq="15min", tz="UTC")
    base = pd.DataFrame({"o": [10, 11, 12, 13, 13.0, 13.4, 13.2], "h": [11, 12, 13, 13.5, 13.6, 14.2, 13.3],
                         "l": [9.9, 10.9, 11.9, 12.9, 12.8, 13.1, 12.6], "c": [11, 12, 13, 13.0, 13.4, 13.2, 12.7],
                         "v": 1.0}, index=idx)
    closes = data.ns_index(idx + pd.Timedelta(minutes=15))
    last_h1_close = pd.Timestamp("2025-07-15 10:00", tz="UTC")
    now = pd.Timestamp("2025-07-15 10:47", tz="UTC")
    f = lv.forming_candle(base, closes, last_h1_close, last_h1_close + pd.Timedelta(hours=1), now)
    assert (f["o"], f["h"], f["l"], f["c"]) == (13.0, 14.2, 12.6, 12.7) and f["built_from_candles"] == 3
    assert f["minutes_to_close"] == 13 and f["direction"] == "down" and f["closes_at"] == "2025-07-15T11:00:00Z"
    zs = [{"level": 12.9, "timeframes": ["1h"]}, {"level": 14.0, "timeframes": ["4h", "1h"]},
          {"level": 12.65, "timeframes": ["15m"]}, {"level": 20.0, "timeframes": ["15m"]}]
    notes = lv.forming_vs_lines(f, zs, "1h")
    assert len(notes) == 3 and "CONFIRMS a break below 12.900 (1h)" in notes[0]
    assert "only a WICK through 14.000 (4h+1h) from below" in notes[1] and "from above" in notes[2]
    assert lv.forming_candle(base, closes, pd.Timestamp("2025-07-15 11:00", tz="UTC"), now, now) is None


def contexts(df15):
    out = {"15m": st.context_from_frame(None, "15m", df15, df15.index + pd.Timedelta(minutes=15))}
    end = df15.index[-1] + pd.Timedelta(minutes=15)
    for iv, hrs in (("1h", 1), ("4h", 4)):
        h = resample_ohlc(df15, "1h")
        if iv == "4h":
            h = resample_ohlc(h, "4h")
        h = h[h.index + pd.Timedelta(hours=hrs) <= end]                             # closed candles only
        out[iv] = st.context_from_frame(None, iv, h, h.index + pd.Timedelta(hours=hrs))
    return out


def episode_keys(res, upto):
    out = []
    for e in res["_episodes"]:
        if e["resolved_idx"] is not None and e["resolved_idx"] <= upto:
            ft = e["follow_through_pips"] if e["resolved_idx"] + lv.FOLLOW_N + lv.RESOLVE_WITHIN <= upto else None
            out.append((round(e["level"], 6), e["idx"], e["resolved_idx"], e["type"], tuple(e["confluence"]),
                        e["prior_holds"], e["flipped"], e["is_retest"], e["approach"],
                        None if ft is None else round(ft, 6)))
    return sorted(out)


def test_no_lookahead_in_zones_reactions_and_features():
    df = walk(4000, seed=13)
    T = 2600
    now_full = df.index[-1] + pd.Timedelta(minutes=15)
    now_cut = df.index[T] + pd.Timedelta(minutes=15)
    full = lv.analyze_levels(contexts(df), 0.1, now_full)
    cut = lv.analyze_levels(contexts(df.iloc[:T + 1]), 0.1, now_cut)
    pert = df.copy()
    pert.iloc[T + 1:, :4] += 300.0
    moved = lv.analyze_levels(contexts(pert), 0.1, now_full)
    a, b, c = episode_keys(full, T), episode_keys(cut, T), episode_keys(moved, T)
    assert len(a) > 300 and a == b == c
    assert {k[4] for k in a} >= {("15m",), ("1h", "15m")}                           # confluence buckets occur


def test_analyze_levels_outputs_on_random_walk():
    df = walk(4000, seed=13)
    now = df.index[-1] + pd.Timedelta(minutes=15)
    res = lv.analyze_levels(contexts(df), 0.1, now)
    last = res["last_close"]
    assert all(z["level"] > last and z["role"] == "resistance" for z in res["zones_above"])
    assert all(z["level"] <= last and z["role"] == "support" for z in res["zones_below"])
    assert 1 <= len(res["zones_above"]) <= 4 and 1 <= len(res["zones_below"]) <= 4
    d = [abs(z["distance_pips"]) for z in res["zones_below"]]
    assert d == sorted(d)
    s = res["statistics"]
    assert s["overall"]["n"] == s["resolved_touches"] > 300
    assert sum(v["n"] for v in s["by_confluence"].values()) == s["overall"]["n"]
    assert set(s["by_prior_respected_touches"]) <= {"0", "1", "2", "3+"}
    assert set(s["by_session"]) <= {"Asia", "London", "New York", "off-session"}
    z = (res["zones_above"] + res["zones_below"])[0]
    assert z["score"] == pytest.approx(sum(z["components"].values()), abs=0.011) and z["timeframes"]
    assert res["mtf"]["alignment"] in ("all up", "all down", "mixed")
    assert [t["interval"] for t in res["mtf"]["timeframes"]] == ["4h", "1h", "15m"]
    assert res["now"]["odds"] and "held" in res["now"]["odds"][0]["text"]


@pytest.fixture(autouse=True)
def _clean():
    cache.clear()
    yield
    cache.clear()


def test_levels_service_markdown_replay_and_signals(monkeypatch):
    full = walk15()
    monkeypatch.setattr(data, "_live_series", feed(full))
    T = pd.Timestamp("2025-06-24 14:20", tz="UTC")
    with replay(T):
        a = lv.levels(SYM, ["4h", "1h", "15m"], pip=0.01)
        sig = st.signals(SYM, ["1h", "15m"])
    assert a["as_of"] == "2025-06-24T14:15:00Z" and a["base_interval"] == "15m" and "_zones" not in a
    h1 = next(t for t in a["mtf"]["timeframes"] if t["interval"] == "1h")
    assert h1["forming"]["built_from_candles"] == 1 and h1["forming"]["minutes_to_close"] == 40
    h4 = next(t for t in a["mtf"]["timeframes"] if t["interval"] == "4h")
    assert h4["forming"] and h4["forming"]["closes_at"] == "2025-06-24T17:00:00Z"   # NY-aligned H4 bin
    md = a["markdown"]
    assert "| Zone | TFs |" in md and "MTF stack" in md and "Hold vs break" in md and len(md.split()) < 750
    assert "level_touch" not in sig["errors"]
    # the future does not change the replayed answer
    monkeypatch.setattr(data, "_live_series", feed(full[full.index + pd.Timedelta(minutes=15) <= T]))
    cache.clear()
    with replay(T):
        b = lv.levels(SYM, ["4h", "1h", "15m"], pip=0.01)
    assert a["zones_above"] == b["zones_above"] and a["statistics"] == b["statistics"] and a["mtf"] == b["mtf"]


def test_approach_uses_only_candles_before_the_touch():
    o = np.array([105.0, 104.8, 104.6, 104.4, 102.0, 101.9])
    c = np.array([104.8, 104.6, 104.4, 102.0, 101.9, 99.0])
    imp = np.array([False, False, False, True, False, True])
    assert lv.approach_type(o, c, imp, 5, 1, 1.0) == "impulse"          # impulse candle 3 fell toward the zone
    imp2 = np.array([False] * 5 + [True])                               # only the touch candle itself is an impulse
    assert lv.approach_type(o, c, imp2, 5, 1, 1.0) == "impulse"         # ... but 2.7 ATR travelled in candles 2-4
    assert lv.approach_type(o, c, imp2, 5, 1, 2.0) == "drift"           # touch candle's own size never counts
    assert lv.approach_type(o, c, imp, 5, -1, 5.0) == "drift"           # impulse was not toward a zone above


def test_shuffle_keeps_candles_and_hours_but_not_order():
    df = walk(2000, seed=3)
    a, b = lv.shuffled_base(df, 1), lv.shuffled_base(df, 1)
    pd.testing.assert_frame_equal(a, b)                                           # deterministic per seed
    assert len(a) == len(df) - 1 and (a["h"] >= a[["o", "c"]].max(axis=1) - 1e-9).all()
    real = np.log(df["c"] / df["c"].shift(1)).dropna()
    shuf = np.log(a["c"] / a["c"].shift(1)).dropna()
    for hr in (0, 9, 15):                                                         # same returns inside each hour-of-day
        x = np.sort(real[real.index.hour == hr].to_numpy())
        y = np.sort(np.log(a["c"] / np.concatenate([[df["c"].iloc[0]], a["c"].to_numpy()[:-1]]))[a.index.hour == hr])
        assert np.allclose(x, y)
    assert not np.allclose(real.to_numpy()[:200], shuf.to_numpy()[:200] if len(shuf) >= 200 else 0)


def test_null_comparison_flags_only_clear_differences():
    stats = {"overall": {"n": 1000, "p_hold": 0.53}, **{d: {} for d in lv.DIMENSIONS}}
    stats["by_session"] = {"Asia": {"n": 800, "p_hold": 0.60}, "London": {"n": 400, "p_hold": 0.52},
                           "New York": {"n": 20, "p_hold": 0.9}}
    null = {"method": "m", "overall": {"n": 2000, "p_hold": 0.52}, **{d: {} for d in lv.DIMENSIONS}}
    null["by_session"] = {"Asia": {"n": 1600, "p_hold": 0.50}, "London": {"n": 800, "p_hold": 0.50},
                          "New York": {"n": 40, "p_hold": 0.5}}
    lv.attach_null(stats, null)
    assert stats["by_session"]["Asia"]["beats_null"] and stats["by_session"]["Asia"]["vs_null_z"] > 4
    assert not stats["by_session"]["London"]["beats_null"] and not stats["by_session"]["New York"]["beats_null"]
    assert not stats["overall"]["beats_null"] and stats["null_benchmark"]["overall_p_hold"] == 0.52


def test_random_walk_has_no_real_edge_against_its_own_null():
    df = walk(4000, seed=13)
    ctxs = contexts(df)
    null = lv.null_benchmark(df, ["4h", "1h", "15m"], 0.1, shuffles=1)
    res = lv.analyze_levels(ctxs, 0.1, df.index[-1] + pd.Timedelta(minutes=15), null=null)
    assert res["statistics"]["null_benchmark"]["overall_n"] > 300
    assert not res["statistics"]["overall"]["beats_null"]
    assert sum(e["real_edge"] for e in res["edges"]) <= 1                         # at most a chance hit


def test_retired_zone_does_not_absorb_new_levels():
    # price chops through 100 four times (zone retired), then a NEW swing level prints at the same price
    rows = [(101, 101.2, 100.9, 101.0)]
    for _ in range(4):
        rows += [(101, 101.1, 98.9, 99.0), (99, 99.1, 98.6, 98.8), (98.8, 101.1, 98.7, 101.0), (101, 101.4, 100.9, 101.2)]
    rows += [(101.2, 101.3, 100.9, 101.0)] * 6
    o, h, l, c, closes = frame(rows)
    known_late = closes[len(rows) - 3]
    lvls = [Level(100.0, "L", "HL", T0 - pd.Timedelta(hours=1), T0, "15m"),
            Level(100.05, "L", "HL", closes[-6], known_late, "15m")]
    arrays = (o, h, l, c, np.ones(len(c)), np.zeros(len(c), bool), 0.1)
    zs = lv.build_zones(lvls, closes, np.ones(len(c)) * 0.8, "15m", arrays=arrays)
    assert len(zs) == 2 and zs[0].get("retired_pos") is not None                   # fresh line, not merged
    zs2 = lv.build_zones(lvls, closes, np.ones(len(c)) * 0.8, "15m")               # without the check they merge
    assert len(zs2) == 1
