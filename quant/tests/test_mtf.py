import numpy as np
import pandas as pd
import pytest

from app import data, mtf
from app import structure as st
from app.cache import cache
from app.data import replay

from .test_retest import walk
from .test_story_replay import SYM, feed, walk15


def frame(rows, start="2025-07-14 00:00", freq="h"):
    a = np.array(rows, float)
    idx = pd.date_range(start, periods=len(a), freq=freq, tz="UTC")
    return pd.DataFrame({"o": a[:, 0], "h": a[:, 1], "l": a[:, 2], "c": a[:, 3], "v": 1.0}, index=idx)


def from_closes(closes, wick=0.2, wicks=None):
    rows, prev = [], closes[0]
    for i, c in enumerate(closes):
        o, hi, lo = prev, max(prev, c) + wick, min(prev, c) - wick
        if wicks and i in wicks:
            hi, lo = max(hi, wicks[i].get("h", hi)), min(lo, wicks[i].get("l", lo))
        rows.append((o, hi, lo, c))
        prev = c
    return frame(rows)


def ctx_of(df, iv="1h"):
    return st.context_from_frame(None, iv, df, df.index + pd.Timedelta(hours=INTERVAL_H[iv]))


INTERVAL_H = {"15m": 0.25, "1h": 1, "4h": 4}
LEAD = [200 + 0.5 * (i % 2) + 0.05 * i for i in range(20)]          # quiet drift up (ATR ~1)
IMPULSE = [197, 193, 189, 185, 181]                                  # 5 big bearish bodies
RANGE = [183, 185, 187, 185, 183, 185, 186.5, 184.5, 183.5, 185, 186, 185, 184.5]
BASE = LEAD + IMPULSE + RANGE                                        # 38 candles, last index 37
N0 = len(BASE)


def test_box_forms_after_the_impulse_and_excludes_it():
    ctx = ctx_of(from_closes(BASE))
    s = mtf.box_states(ctx)
    # nothing while only the impulse low and one bounce exist; the pre-impulse high (201.45) is never a box edge
    assert (s.status[:32] == mtf.NONE).all() and np.nanmax(s.top) == 187.0
    # first box = impulse low 181 .. first bounce high 187 (3 swings), then redrawn from the latest two lows
    assert (s.status[32], s.top[32], s.bot[32]) == (mtf.INSIDE, 187.0, 181.0)
    assert (s.top[37], s.bot[37]) == (187.0, 183.0)
    ep = s.episodes[0]
    assert (ep["outer_top"], ep["outer_bot"], ep["lead_impulse"], ep["end_idx"]) == (187.0, 181.0, -1, None)
    tf = mtf.read_timeframe(ctx, pip=0.1)
    assert tf["regime"] == "consolidation" and tf["box"]["position"]["state"] == "inside"
    assert tf["box"]["lead_in_impulse"] == "down"
    li = tf["lead_in_impulse"]
    assert li["direction"] == "down" and li["to"] == 181.0 and li["pips"] > 190
    assert "no 1h body has closed above 187.000" in tf["box"]["facts"][0]
    assert tf["box"]["first_box"]["bottom"] == 181.0
    assert "BODY close below 183.000" in tf["continuation"]["down"] and "sweep" in tf["continuation"]["up"]


def test_wick_sweep_is_not_a_break_but_a_body_close_is():
    # the newest swings redraw the box to 183–186.5 for candle 38
    # candle 38: wick to 188.6 (21 pips above the 186.5 top) but the body closes back at 185.5 -> sweep
    df = from_closes(BASE + [185.5], wicks={N0: {"h": 188.6}})
    ctx = ctx_of(df)
    s = mtf.box_states(ctx)
    assert s.status[-1] == mtf.INSIDE and s.breaks == []
    tf = mtf.read_timeframe(ctx, 0.1)
    sw = [x for x in tf["sweeps"] if x["side"] == "high"]
    assert len(sw) == 1 and sw[0]["wick_extreme"] == 188.6 and sw[0]["pips_beyond"] == pytest.approx(21.0)
    assert sw[0]["close"] == 185.5 and tf["box"]["closed_above_top"] is False
    sig = mtf.range_signals(ctx, 0.1)
    assert [x["type"] for x in sig] == ["range_sweep"] and sig[0]["side"] == "high" and sig[0]["direction"] == "down"
    # a body close above 186.5 (+tolerance) is a break; price inside the box before, so the state flips
    ctx2 = ctx_of(from_closes(BASE + [188.5]))
    s2 = mtf.box_states(ctx2)
    assert s2.status[-1] == mtf.OUT_UP and len(s2.breaks) == 1 and s2.breaks[0]["edge"] == 186.5
    tf2 = mtf.read_timeframe(ctx2, 0.1)
    assert tf2["box"]["position"]["state"] == "closed_above" and tf2["box"]["position"]["candles_ago"] == 0
    assert tf2["box"]["position"]["pips_beyond"] == pytest.approx(20.0)
    assert mtf.range_signals(ctx2, 0.1) == []
    # a marginal close (inside the 0.1 ATR tolerance) does not count
    ctx3 = ctx_of(from_closes(BASE + [186.6]))
    assert mtf.box_states(ctx3).status[-1] == mtf.INSIDE


def test_fakeout_vs_real_break():
    # body closes below 183 for two candles, then back inside -> fakeout (N=3); later a break that stays out
    df = from_closes(BASE + [182.0, 182.4, 184.5, 185.5, 186.0, 185.0])
    ctx = ctx_of(df)
    s = mtf.box_states(ctx)
    b = s.breaks[0]
    assert (b["idx"], b["side"], b["edge"], b["back_idx"]) == (N0, -1, 183.0, N0 + 2) and mtf.is_fakeout(b)
    assert not mtf.is_fakeout(b, fakeout_max=1)
    tf = mtf.read_timeframe(ctx, 0.1)
    fo = tf["fakeouts"]
    assert len(fo) == 1 and fo[0]["side"] == "below" and fo[0]["candles_until_back_inside"] == 2
    assert fo[0]["close_beyond_pips"] == pytest.approx(10.0) and fo[0]["followed"]["reached_mid"]
    assert tf["box"]["fakeouts_below"] == 1 and tf["box"]["fakeouts_above"] == 0
    assert tf["box"]["position"]["state"] == "inside"
    # real break: closes below and stays below for MAX_BROKEN candles -> the box is history
    tail = [181.5] + [180.5 - 0.1 * i for i in range(mtf.MAX_BROKEN + 2)]
    ctx2 = ctx_of(from_closes(BASE + [182.0, 182.4, 184.5, 185.5] + tail))
    s2 = mtf.box_states(ctx2)
    ep = s2.episodes[0]
    assert ep["resolved"] == "down" and ep["end_idx"] == N0 + 4 and [mtf.is_fakeout(x) for x in ep["breaks"]] == [True, False]
    hist = mtf.box_history(ctx2, s2, 0.1)
    assert hist[0]["resolved"] == "down" and hist[0]["fakeouts_below"] == 1 and hist[0]["lead_in_impulse"] == "down"
    assert not hist[0]["preceded_by_opposite_fakeout"] and (hist[0]["first_top"], hist[0]["first_bottom"]) == (187.0, 181.0)
    edges = mtf.prior_box_edges(ctx2, s2, last=182.0, pip=0.1, atr=2.0)
    assert {e["level"] for e in edges} >= {183.0, 187.0} and all(e["role"] == "resistance" for e in edges if e["level"] > 182)


def stack_ctxs(extra):
    h1 = from_closes(BASE + extra)
    # 15m candles: four per hour, last quarter closes at the hourly close
    rows = []
    for (ts, r) in h1.iterrows():
        for k in range(4):
            a = r["o"] + (r["c"] - r["o"]) * k / 4
            b = r["o"] + (r["c"] - r["o"]) * (k + 1) / 4
            rows.append((a, max(a, b) + 0.05, min(a, b) - 0.05, b))
    m15 = frame(rows, freq="15min")
    return {"1h": ctx_of(h1), "15m": ctx_of(m15, "15m")}


def test_playbook_range_zones_and_cases():
    res = mtf.analyze_mtf(stack_ctxs([185.5]), pip=0.1, sl_pips=5, tp_pips=10, tp2_pips=60)
    pb = res["playbook"]
    assert pb["mode"] == "range" and pb["timeframe"] == "1h"
    s, b = pb["sell_zone"], pb["buy_zone"]
    assert s["high"] == 186.5 and b["low"] == 183.0 and s["low"] < 186.5 < s["stop"] and b["stop"] < 183.0 < b["high"]
    assert s["stop"] == pytest.approx(187.0) and b["stop"] == pytest.approx(182.5)
    # tp1 = 10 pips; tp2 (60 pips) is capped at the opposite edge (35 pips away)
    assert [t["pips"] for t in s["targets"]] == pytest.approx([10.0, 35.0]) and s["targets"][1]["capped_at"] == "opposite edge"
    assert pb["no_trade_zone"]["low"] == pytest.approx(184.05) and pb["price_position"] == "mid_box"
    acts = [c["action"] for c in pb["cases"]]
    assert acts == ["sell", "buy", "hold", "buy", "sell"] and pb["cases"][0]["entry_price"] == 186.5
    # the sell-zone stop note knows about earlier wicks above the edge
    assert isinstance(s["stop_inside_prior_sweeps"], bool) and s["wick_extreme"] >= 186.5
    assert set(pb["cases"][0]) == {"action", "when", "entry", "entry_price", "stop", "targets", "trigger_tf", "reason",
                                   "risk"}
    assert res["stack"]["context"]["interval"] == "1h" and res["stack"]["trigger"]["interval"] == "15m"
    assert any("Plan (range)" in r for r in pb["reasons"])


def test_playbook_break_retest_cases():
    res = mtf.analyze_mtf(stack_ctxs([181.8, 181.6]), pip=0.1, sl_pips=5, tp_pips=10, tp2_pips=20)
    pb = res["playbook"]
    assert pb["mode"] == "break_retest" and pb["direction"] == "short" and pb["broken_edge"] == 183.0
    assert pb["stop"] == pytest.approx(183.5) and pb["break"]["candles_ago"] == 1
    assert pb["retest_zone"]["low"] < 183.0 <= pb["retest_zone"]["high"]
    cases = pb["cases"]
    assert [c["action"] for c in cases] == ["sell", "hold", "hold", "buy", "hold"]
    assert cases[0]["entry_price"] == 183.0 and cases[0]["stop"] == pytest.approx(183.5) and cases[0]["trigger_tf"] == "15m"
    assert "BODY closes back below 183.000" in cases[0]["when"]
    assert "closes back above 183.000" in cases[3]["when"] and cases[3]["stop"] == pytest.approx(182.5)
    assert [t["name"] for t in cases[3]["targets"]] == ["box_mid", "opposite_edge"]
    tgt = {t["name"]: t["price"] for t in cases[0]["targets"]}
    assert tgt["tp1"] == pytest.approx(182.0) and tgt["measured_move"] == pytest.approx(179.5)
    assert tgt["prior_impulse_extreme"] == 181.0                      # the lead-in impulse low
    assert "closed BELOW" in res["stack"]["reading"]


def test_no_lookahead_box_states_and_events():
    df = walk(3000, seed=5)
    idx = df.index
    full = st.context_from_frame(None, "15m", df, idx + pd.Timedelta(minutes=15))
    T = 2000
    cut = st.context_from_frame(None, "15m", df.iloc[:T + 1], idx[:T + 1] + pd.Timedelta(minutes=15))
    a, b = mtf.box_states(full), mtf.box_states(cut)
    for name in ("top", "bot", "status", "break_idx", "start"):
        np.testing.assert_array_equal(getattr(a, name)[:T + 1], getattr(b, name))
    key = lambda x: (x["idx"], x["side"], x["edge"], x["attempt"], x["imp_dir"])   # noqa: E731
    assert [key(x) for x in a.breaks if x["idx"] <= T] == [key(x) for x in b.breaks]
    assert len(b.breaks) > 20


def test_odds_tables_and_baseline_on_random_walk():
    df = walk(6000, seed=9)
    ctx = st.context_from_frame(None, "15m", df, df.index + pd.Timedelta(minutes=15))
    o = mtf.timeframe_odds(ctx, pip=0.1, tp_pips=10, shuffles=1)
    ev = o["events"]
    assert set(ev) >= {"edge_fade_to_mid", "sweep_to_mid", "sweep_to_opposite_edge", "break_continues_tp",
                       "break_continues_1atr", "break_is_fakeout", "fakeout_reaches_opposite_edge",
                       "fakeout_then_breaks_opposite_side", "real_break_retests_edge"}
    for k, e in ev.items():
        assert e["n"] > 30 and 0 <= e["rate"] <= 1 and e["ci95"][0] <= e["rate"] <= e["ci95"][1]
        assert e["baseline_rate"] is not None and e["verdict"] in (
            "no edge (same as random candles)", "higher than random candles", "lower than random candles")
    assert sum(e["verdict"].startswith("no edge") for e in ev.values()) >= len(ev) - 1   # random walk: no edge
    assert any(k.startswith("attempt=") for k in o["fakeout_splits"]) and "body=<1 ATR" in o["fakeout_splits"]
    rd = o["retest_depth_pips"]
    assert rd["n"] > 10 and 0 <= rd["median"] <= rd["p80"]
    assert mtf.compare({"n": 400, "rate": 0.70, "ci95": [0, 1]}, {"n": 800, "rate": 0.50, "ci95": [0, 1]})["verdict"] \
        .startswith("higher")
    assert mtf.compare({"n": 10, "rate": 0.9, "ci95": [0, 1]}, {"n": 800, "rate": 0.5, "ci95": [0, 1]})["verdict"] == \
        "insufficient data"


@pytest.fixture(autouse=True)
def _clean():
    cache.clear()
    yield
    cache.clear()


def test_mtf_service_replay_and_markdown(monkeypatch):
    full = walk15()
    monkeypatch.setattr(data, "_live_series", feed(full))
    T = pd.Timestamp("2025-06-24 14:20", tz="UTC")
    with replay(T):
        a = mtf.mtf(SYM, ["4h", "1h", "15m"], pip=0.01)
    assert a["as_of"] == "2025-06-24T14:15:00Z" and a["params"]["intervals"] == ["4h", "1h", "15m"]
    assert set(a) >= {"timeframes", "stack", "playbook", "odds", "needs", "markdown", "data_note", "prior_box_edges"}
    assert a["playbook"]["mode"] in ("range", "break_retest", "trend_pullback", "stand_aside")
    assert a["playbook"]["cases"] and a["playbook"]["reasons"]
    assert set(a["stack"]) >= {"context", "setup", "trigger", "reading"} and a["stack"]["context"]["interval"] == "4h"
    for tf in a["timeframes"].values():
        assert tf["regime"] in ("consolidation", "impulse_up", "impulse_down", "trend_up", "trend_down")
        assert {"fakeouts", "history", "sweeps", "continuation", "swings"} <= set(tf)
        assert pd.Timestamp(tf["last_closed_candle_close"]) <= T
    assert "17:00 New York" in a["data_note"]["h4_grid"] and len(a["markdown"].splitlines()) < 60
    monkeypatch.setattr(data, "_live_series", feed(full[full.index + pd.Timedelta(minutes=15) <= T]))
    cache.clear()
    with replay(T):
        b = mtf.mtf(SYM, ["4h", "1h", "15m"], pip=0.01)
    assert a["timeframes"] == b["timeframes"] and a["playbook"] == b["playbook"] and a["odds"] == b["odds"]


def test_stale_context_break_hands_the_plan_to_the_setup_box():
    box_in = {"top": 10.0, "bottom": 9.0, "position": {"state": "inside"}}
    old = {"top": 20.0, "bottom": 19.0, "position": {"state": "closed_below", "candles_ago": 20, "edge": 19.0}}
    fresh = {"top": 20.0, "bottom": 19.0, "position": {"state": "closed_below", "candles_ago": 2, "edge": 19.0}}
    setup = {"interval": "1h", "box": box_in, "last_close": 9.5, "atr": 0.5}
    assert mtf._playbook_box({"interval": "4h", "box": old, "last_close": 9.5, "atr": 1.0}, setup) is setup
    ctx = {"interval": "4h", "box": fresh, "last_close": 18.5, "atr": 1.0}
    assert mtf._playbook_box(ctx, setup) is ctx
    far = {"interval": "4h", "box": fresh, "last_close": 9.5, "atr": 1.0}       # 9.5 ATR away from the edge
    assert mtf._playbook_box(far, setup) is setup
    assert mtf._playbook_box({"interval": "4h", "box": None, "last_close": 1, "atr": 1}, {"interval": "1h", "box": None,
                                                                                         "last_close": 1, "atr": 1}) is None
