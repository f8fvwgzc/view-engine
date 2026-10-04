import numpy as np
import pandas as pd
import pytest

from app import retest as rt
from app import structure as st
from app.data import resample_ohlc
from app.symbols import normalize

from .test_structure import candles_from_closes

IDX = pd.date_range("2025-07-14 00:00", periods=8, freq="15min", tz="UTC")
CLOSES = IDX + pd.Timedelta(minutes=15)


def arrays(rows):
    a = np.array(rows, float)
    return a[:, 0], a[:, 1], a[:, 2], a[:, 3]


def level(price=100.0, kind="H", known=IDX[0], label="HH"):
    return rt.Level(price, kind, label, IDX[0] - pd.Timedelta(hours=3), known, "1h")


CONTINUE = [  # o, h, l, c
    (99, 99.5, 98.5, 99), (99, 101.2, 98.9, 101),        # 1: body close above 100 -> break up
    (101, 101.5, 100.5, 101.2),                          # 2: no touch
    (101.2, 101.3, 99.7, 100.6),                         # 3: wick 3 pips through, closes back above -> rejection
    (100.7, 102, 100.5, 101.8), (101.8, 104, 101.7, 103.9),
    (103.9, 104, 103.5, 103.8), (103.8, 104, 103.6, 103.9)]
FAIL = CONTINUE[:3] + [(101.2, 101.3, 99.7, 99.5),      # 3: touches and CLOSES back through the level
                       (99.5, 99.6, 98.5, 98.7), (98.7, 98.9, 98, 98.2), (98.2, 98.3, 98, 98.1),
                       (98.1, 98.2, 98, 98.1)]


def run(rows, lv, sl=10, tp=30, spread=1.0, k=4, n=None):
    o, h, l, c = arrays(rows)
    ev, pend = rt.find_retests(o, h, l, c, CLOSES[:len(c)], [lv], 0.1, 3, 48)
    tr = rt.build_trades(ev, o, h, l, c, CLOSES[:len(c)], IDX[:len(c)], 0.1, sl, tp, spread, k)
    return ev, pend, tr


def test_break_retest_continue_three_entries():
    ev, pend, tr = run(CONTINUE, level())
    e = ev[0]
    assert (e["break_idx"], e["touch_idx"], e["fill_idx"], e["rejected"]) == (1, 3, 3, True)
    assert e["wick_depth_pips"] == pytest.approx(3.0)
    touch, rej, nxt = tr["touch"][0], tr["reject_close"][0], tr["next_open"][0]
    assert touch["entry"] == 100 and touch["stop"] == pytest.approx(99) and touch["target"] == pytest.approx(103)
    assert rej["entry"] == 100.6 and nxt["entry"] == 100.7
    for t in (touch, rej, nxt):
        assert t["outcome"] == "win" and t["pips"] == pytest.approx(30 - 1.0) and t["direction"] == "long"
        assert t["level"] == 100 and t["level_type"] == "HH" and t["level_interval"] == "1h"
    assert rej["entry_time"] == "2025-07-14T01:00:00Z"   # close of candle 3
    assert nxt["entry_time"] == "2025-07-14T01:00:00Z"   # open of candle 4


def test_break_retest_fail():
    ev, _, tr = run(FAIL, level())
    assert ev[0]["touch_idx"] == 3 and not ev[0]["rejected"]
    assert tr["reject_close"] == [] and tr["next_open"] == []          # close back through cancels them
    t = tr["touch"][0]                                                 # the resting limit is filled and stopped
    assert t["outcome"] == "loss" and t["pips"] == pytest.approx(-10 - 1.0)


def test_short_mirror():
    rows = [(200 - o, 200 - l, 200 - h, 200 - c) for o, h, l, c in CONTINUE]
    ev, _, tr = run(rows, level(100.0, "L", label="LL"))
    assert ev[0]["d"] == -1 and ev[0]["rejected"] and ev[0]["wick_depth_pips"] == pytest.approx(3.0)
    assert tr["reject_close"][0]["direction"] == "short" and tr["reject_close"][0]["outcome"] == "win"


def test_level_not_usable_before_it_is_known():
    # level only known after candle 1 closed -> that cross does not count, and no later genuine cross exists
    ev, pend, _ = run(CONTINUE, level(known=CLOSES[1]))
    assert ev == [] and pend == []          # price already beyond: neither broken-by-rule nor awaiting
    ev, pend, _ = run(CONTINUE[:1], level())
    assert ev == [] and pend[0]["status"] == "awaiting_break"
    ev, pend, _ = run(CONTINUE[:3], level())
    act = rt.active_levels(ev, pend, arrays(CONTINUE[:3])[3], 0.1, 3)
    assert act[0]["status"] == "broken_awaiting_retest" and act[0]["distance_pips"] == pytest.approx(-12)
    ev, pend, _ = run(CONTINUE[:4], level())
    assert rt.active_levels(ev, pend, arrays(CONTINUE[:4])[3], 0.1, 4)[0]["status"] == "retest_in_progress"


def test_simulate_fixed_rules():
    h = np.array([10.2, 10.6, 11.2]); l = np.array([9.9, 9.4, 10.5]); c = np.array([10.1, 10.5, 11.0])
    assert rt.simulate_fixed(h, l, c, 0, 1, 10.0, 0.5, 0.5, 3)["outcome"] == "loss"      # candle 1: both -> loss
    assert rt.simulate_fixed(h, l, c, 0, 1, 10.0, 1.0, 1.0, 3)["outcome"] == "win"       # candle 2 high 11.2
    r = rt.simulate_fixed(h, l, c, 0, 1, 10.0, 2.0, 2.0, 3)
    assert r["outcome"] == "timeout" and r["move"] == pytest.approx(1.0)
    assert rt.simulate_fixed(h, l, c, 0, 1, 10.0, 2.0, 2.0, 5) is None                   # window not complete
    # touch entry: target on the fill candle is ignored, the stop is not
    assert rt.simulate_fixed(h, l, c, 0, 1, 10.0, 5.0, 0.15, 1, skip_target_on_first=True)["outcome"] == "timeout"
    assert rt.simulate_fixed(h, l, c, 0, 1, 10.0, 0.05, 5.0, 1, skip_target_on_first=True)["outcome"] == "loss"


def test_breakeven_and_defaults():
    assert rt.breakeven_fixed(25, 75, 2.5) == pytest.approx(0.275)
    assert rt.default_pip(normalize("XAUUSD")) == 0.1 and rt.default_pip(normalize("USDJPY")) == 0.01
    assert rt.default_pip(normalize("EURUSD")) == 0.0001
    assert rt.default_spread_price(normalize("XAUUSD")) / 0.1 == pytest.approx(2.5)
    assert rt.default_spread_price(normalize("XAUUSD")) / 1.0 == pytest.approx(0.25)
    assert rt.default_spread_price(normalize("EURUSD")) / 0.0001 == pytest.approx(1.0)


def test_excursion_and_percentile_math():
    assert rt.percentiles([1, 2, 3, 4, 5]) == pytest.approx({"p50": 3, "p70": 3.8, "p80": 4.2, "p90": 4.6,
                                                             "p95": 4.8})
    assert rt.percentiles([]) is None
    o, h, l, c = arrays(CONTINUE)
    ev, _ = rt.find_retests(o, h, l, c, CLOSES, [level()], 0.1, 3, 48)
    exc = rt.excursions(ev, h, l, 0.1, tp_pips=30, k=4)[0]     # window = candles 3..6
    assert exc["reached_tp"] and exc["bars_to_tp"] == 3        # candle 5 trades 103 = level + 30 pips
    assert exc["mae_pips"] == pytest.approx(3.0)               # deepest wick below the level before that
    assert exc["mfe_pips"] == pytest.approx(40.0)
    exc2 = rt.excursions(ev, h, l, 0.1, tp_pips=60, k=4)[0]    # never reaches +60 pips
    assert not exc2["reached_tp"] and exc2["mae_pips"] == pytest.approx(3.0)
    assert rt.excursions(ev, h, l, 0.1, tp_pips=30, k=9) == [] # incomplete window is skipped
    rep = rt.excursion_report([{"mae_pips": m, "mfe_pips": 50, "reached_tp": m < 20, "bars_to_tp": 3}
                               for m in (2, 4, 6, 8, 30)])
    assert rep["reached_tp"]["n"] == 4 and rep["reached_tp"]["wick_penetration_pips"]["p50"] == pytest.approx(5)
    surv = {r["stop_pips_beyond_level"]: r["eventual_winners_kept"] for r in rep["stop_survival"]}
    assert surv[10] == 1.0 and rep["all_retests"]["wick_penetration_pips"]["p50"] == pytest.approx(6)


def walk(n=4000, seed=7):
    rng = np.random.default_rng(seed)
    c = 2000 + np.cumsum(rng.normal(0, 0.8, n))
    o = np.concatenate([[c[0]], c[:-1]])
    idx = pd.date_range("2025-03-03", periods=n, freq="15min", tz="UTC")
    return pd.DataFrame({"o": o, "h": np.maximum(o, c) + rng.uniform(0, 0.6, n),
                         "l": np.minimum(o, c) - rng.uniform(0, 0.6, n), "c": c, "v": 0.0}, index=idx)


def ctxs(df):
    e = st.context_from_frame(None, "15m", df, df.index + pd.Timedelta(minutes=15))
    hourly = resample_ohlc(df, "1h")
    hourly = hourly[hourly.index + pd.Timedelta(hours=1) <= df.index[-1] + pd.Timedelta(minutes=15)]  # closed only
    lv = st.context_from_frame(None, "1h", hourly, hourly.index + pd.Timedelta(hours=1))
    return e, lv


def trades_upto(df, upto_idx, k=32):
    e, lv = ctxs(df)
    o, h, l, c = (e.df[x].to_numpy(float) for x in ("o", "h", "l", "c"))
    levels = rt.levels_from_context(lv) + rt.levels_from_context(e)
    ev, _ = rt.find_retests(o, h, l, c, e.closes, levels, 0.1, 3, 48)
    tr = rt.build_trades(ev, o, h, l, c, e.closes, e.df.index, 0.1, 25, 75, 2.5, k)
    return {v: [t for t in tl if t["_start"] + k <= upto_idx] for v, tl in tr.items()}


def test_no_lookahead():
    df = walk()
    T = 2500
    full = trades_upto(df, T)
    cut = trades_upto(df.iloc[:T + 1], T)
    pert = df.copy()
    pert.iloc[T + 1:, :4] += 500.0
    moved = trades_upto(pert, T)
    for v in rt.ENTRIES:
        assert len(full[v]) > 30
        assert full[v] == cut[v] == moved[v]


def test_core_grid_shape_and_outputs():
    e, lv = ctxs(walk())
    res = rt.retest_core(e, lv, pip=0.1, sl_pips=25, tp_pips=75, spread_pips=2.5)
    g = res["grid"]
    assert g["cells_tested"] == len(rt.GRID_SL) * len(rt.GRID_TP) == 40 and len(g["cells"]) == 40
    assert {(x["sl_pips"], x["tp_pips"]) for x in g["cells"]} == {(s, t) for s in rt.GRID_SL for t in rt.GRID_TP}
    assert g["best"] is None or g["best"]["n"] >= rt.GRID_MIN_N
    assert "multiple comparisons" in g["warning"]
    assert set(res["entries"]) == set(rt.ENTRIES)
    s = res["entries"]["reject_close"]["summary"]
    assert s["breakeven_win_rate"] == pytest.approx(0.275) and s["expectancy_r"] == pytest.approx(s["expectancy_pips"] / 25)
    for key in ("by_session", "by_level_type", "by_trend_alignment", "by_hour_utc", "by_wick_depth"):
        assert res["entries"]["reject_close"][key]
    f = res["funnel"]
    assert f["broken"] >= f["retested_within_max_wait"] >= f["rejected_close_back_on_break_side"]
    assert res["excursions"]["all_retests"]["n"] > 0 and len(res["active"]) <= 12
    # a random walk must not be called an edge
    assert all(res["entries"][v]["summary"]["verdict"] != "edge" for v in rt.ENTRIES)


def test_retest_signal_on_last_closed_candle():
    df = candles_from_closes([10, 11, 12, 13, 12, 11, 12, 12.5, 13.6, 14, 13.2])
    ctx = st.context_from_frame(None, "1h", df, df.index + pd.Timedelta(hours=1))
    sig = rt.retest_signals(ctx, None, pip=0.1, tol_pips=3)
    assert len(sig) == 1
    s = sig[0]
    assert s["type"] == "retest_long" and s["level"] == 13 and s["level_interval"] == "1h"
    assert s["wick_depth_pips"] == pytest.approx(-1.0) and s["entry_ref"] == 13.2
    assert s["stop"] == pytest.approx(13.2 - 2.5) and s["targets"][1]["price"] == pytest.approx(13.2 + 7.5)
    # one candle earlier nothing fires (no touch yet)
    prev = st.context_from_frame(None, "1h", df.iloc[:-1], df.index[:-1] + pd.Timedelta(hours=1))
    assert rt.retest_signals(prev, None, pip=0.1) == []
