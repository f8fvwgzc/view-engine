import numpy as np
import pandas as pd
import pytest

from app import backtest as bt
from app import structure as st
from app.model import event_features, structure_events

from .test_structure import ZIG, candles_from_closes


def ctx_from(df, interval="1h", unit_atr=False):
    closes = df.index + pd.Timedelta(hours=1)
    ctx = st.context_from_frame(None, interval, df, closes)
    if unit_atr:
        ctx.atr = np.ones(len(df))
    return ctx


def random_walk(n=700, seed=3):
    rng = np.random.default_rng(seed)
    c = 100 + np.cumsum(rng.normal(0, 0.3, n))
    o = np.concatenate([[c[0]], c[:-1]])
    idx = pd.date_range("2025-03-03", periods=n, freq="h", tz="UTC")
    return pd.DataFrame({"o": o, "h": np.maximum(o, c) + rng.uniform(0, 0.2, n),
                         "l": np.minimum(o, c) - rng.uniform(0, 0.2, n), "c": c, "v": 0.0}, index=idx)


def test_wilson_ci_known_values():
    lo, hi = bt.wilson_ci(50, 100)
    assert lo == pytest.approx(0.4038, abs=1e-4) and hi == pytest.approx(0.5962, abs=1e-4)
    lo, hi = bt.wilson_ci(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-3)
    assert bt.wilson_ci(0, 0) == (None, None)


def test_breakeven_and_verdict():
    assert bt.breakeven_win_rate(1.0, 0.05) == pytest.approx(0.525)
    assert bt.breakeven_win_rate(2.0, 0.05) == pytest.approx(0.35)
    be = 0.525
    assert bt.verdict(99, 0.9, be, 0.5) == "insufficient data"
    lo, _ = bt.wilson_ci(70, 100)
    assert bt.verdict(100, lo, be, 0.35) == "edge"
    lo, _ = bt.wilson_ci(55, 100)  # point estimate above breakeven but CI is not
    assert lo < be and bt.verdict(100, lo, be, 0.05) == "no proven edge"


def test_simulate_trade_outcomes():
    c = np.array([10, 10.5, 11, 12.5, 12, 12, 12], float)
    h, l = c + 0.1, c - 0.1
    # long from 10, stop 9, risk 1, target 2R = 12 -> hit at idx 3 (high 12.6)
    r = bt.simulate_trade(h, l, c, 0, 1, 10.0, 9.0, 1.0, 5, 2.0)
    assert r["outcome"] == "win" and r["r"] == 2.0 and r["exit_idx"] == 3
    # short from 10, stop 10.55 -> stopped at idx 1 (high 10.6)
    r = bt.simulate_trade(h, l, c, 0, -1, 10.0, 10.55, 0.55, 5, 1.0)
    assert r["outcome"] == "loss" and r["r"] == -1.0 and r["exit_idx"] == 1
    # same candle touches stop and target -> loss
    h2, l2 = np.array([10, 12.0]), np.array([10, 8.0])
    r = bt.simulate_trade(h2, l2, np.array([10, 10.0]), 0, 1, 10.0, 9.0, 1.0, 1, 1.0)
    assert r["outcome"] == "loss"
    # timeout marked to market: k=2 -> close 11 -> +0.2R on risk 5
    r = bt.simulate_trade(h, l, c, 0, 1, 10.0, 5.0, 5.0, 2, 1.0)
    assert r["outcome"] == "timeout" and r["r"] == pytest.approx(0.2) and r["exit_idx"] == 2
    # not enough future candles and unresolved -> open (excluded)
    assert bt.simulate_trade(h, l, c, 5, 1, 12.0, 5.0, 7.0, 3, 1.0) is None


def test_known_trades_on_zigzag():
    ctx = ctx_from(candles_from_closes(ZIG), unit_atr=True)
    # bos_up at idx 8: close 14 beyond swing-high body 13; protected HL body 11 -> stop 10.8, risk 3.2
    trades = {t["_idx"]: t for t in bt.build_trades(ctx, k=7, target_r=0.9, cost_r=0.05)}
    t8 = trades[8]
    assert t8["types"] == ["bos_up"] and t8["direction"] == "long"
    assert t8["entry"] == 14 and t8["stop"] == pytest.approx(10.8) and t8["target"] == pytest.approx(16.88)
    assert t8["outcome"] == "win" and t8["bars_held"] == 7 and t8["r_net"] == pytest.approx(0.85)
    # shorter horizon: target not reached within 5 candles -> timeout at close 15 = +1/3.2 R
    t8b = {t["_idx"]: t for t in bt.build_trades(ctx, k=5, target_r=0.9, cost_r=0.0)}[8]
    assert t8b["outcome"] == "timeout" and t8b["r"] == pytest.approx(1 / 3.2)


def test_downtrend_short_loses_when_stopped():
    closes = [30 - x for x in ZIG] + [20.0]  # after the last bos_down, price rips to 20 -> stop
    ctx = ctx_from(candles_from_closes(closes), unit_atr=True)
    trades = bt.build_trades(ctx, k=30, target_r=5.0, cost_r=0.05)
    last = trades[-1]
    assert last["direction"] == "short" and last["outcome"] == "loss" and last["r_net"] == pytest.approx(-1.05)


def test_no_lookahead_truncation_and_future_perturbation():
    df = random_walk()
    T, k = 450, 12
    full = ctx_from(df)
    cut = ctx_from(df.iloc[:T + 1])

    def sig(ctx, upto):
        out = []
        for ev in structure_events(ctx) + bt.failed_breakout_events(ctx):
            if ev["t"] > upto:
                continue
            fe = event_features(ctx, ev)
            out.append((ev["t"], ev["type"], ev["dir"], round(ev["level"], 9),
                        None if fe is None else (round(fe["entry"], 9), round(fe["stop"], 9))))
        return sorted(out)
    a, b = sig(full, T), sig(cut, T)
    assert a == b and len(a) > 20  # signals up to T identical with or without the future

    pert = df.copy()
    pert.iloc[T + 1:, :4] *= 1.7  # change everything after T
    tr1 = [t for t in bt.build_trades(full, k) if t["_idx"] + k <= T]
    tr2 = [t for t in bt.build_trades(ctx_from(pert), k) if t["_idx"] + k <= T]
    assert tr1 == tr2 and len(tr1) > 10


def test_failed_breakout_matches_box_status():
    df = random_walk(400, seed=11)
    ctx = ctx_from(df)
    got = {e["t"] for e in bt.failed_breakout_events(ctx)}
    want = set()
    for t in range(2, len(df)):
        s = st.box_status(df, t)
        if s.get("status") == "failed" and s.get("failed_idx") == t:
            want.add(t)
    assert got == want and len(want) > 0


def test_summarize_stats():
    rs = [1, -1, -1, -1, 1, 1, -1, 0.5]
    trades = [{"r": r, "r_net": r - 0.05, "outcome": "win" if r == 1 else "loss" if r == -1 else "timeout",
               "entry_time": f"t{i}"} for i, r in enumerate(rs)]
    s = bt.summarize(trades, 1.0, 0.05, full=True)
    assert s["n_trades"] == 8 and s["wins"] == 3 and s["losses"] == 4 and s["timeouts"] == 1
    assert s["win_rate"] == pytest.approx(3 / 8)
    assert s["avg_r"] == pytest.approx(sum(rs) / 8) and s["expectancy_r"] == pytest.approx(sum(rs) / 8 - 0.05)
    assert s["total_r"] == pytest.approx(sum(rs) - 0.4)
    assert s["longest_losing_streak"] == 3
    assert s["max_drawdown_r"] == pytest.approx(3 * 1.05)          # 0.95 -> -2.2
    assert s["profit_factor"] == pytest.approx((3 * 0.95 + 0.45) / (4 * 1.05))
    assert s["verdict"] == "insufficient data" and s["period_start"] == "t0" and s["period_end"] == "t7"


def test_run_backtest_shape_and_curve_downsample():
    ctx = ctx_from(random_walk(3000, seed=5))
    res = bt.run_backtest(ctx, k=24, target_r=1.0, cost_r=0.05)
    o = res["overall"]
    assert o["n_trades"] > 200 and len(res["equity_curve"]) <= 200
    assert res["equity_curve"][-1]["n"] == o["n_trades"]
    assert res["equity_curve"][-1]["cum_r"] == pytest.approx(o["total_r"], abs=1e-2)
    assert len(res["recent_trades"]) == 20 and "_idx" not in res["recent_trades"][0]
    for key in ("by_type", "by_session", "by_htf_alignment", "by_impulse", "by_day_of_week", "by_box_width"):
        assert res[key]
    assert sum(v["n_trades"] for v in res["by_impulse"].values()) == o["n_trades"]
    assert "failed_breakout" in res["by_type"]
    # a random walk must not be declared an edge
    assert o["verdict"] != "edge"


def test_stops_match_realtime_state_at_every_signal():
    """The stop used in the backtest must equal what was knowable at the signal candle's close
    (rebuild the structure on data truncated at that candle and compare)."""
    df = random_walk(900, seed=21)
    full = ctx_from(df)
    trades = bt.build_trades(full, k=12)
    assert len(trades) > 80
    for tr in trades[::3]:
        t = tr["_idx"]
        cut = ctx_from(df.iloc[:t + 1])
        d = 1 if tr["direction"] == "long" else -1
        fe = event_features(cut, {"t": t, "dir": d, "level": tr["entry"], "box": None})
        assert fe is not None and fe["stop"] == pytest.approx(tr["stop"]), (t, tr["types"])
