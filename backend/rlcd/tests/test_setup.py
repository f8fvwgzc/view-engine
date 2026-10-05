import numpy as np
import pytest

from rlcd import policy as P
from rlcd.contract import TrainRequest
from rlcd.heads import setup
from rlcd.quant import QuantClient
from rlcd.store import Store
from rlcd.training import load_setup_data, split_by_time
from tests.conftest import make_client, train
from tests.synth import BULL, BULL_NO_PATTERN, NAMES, SynthQuant


# ----------------------------------------------------------------------------- splitting

def two_symbols(n=600, horizon=12):
    """Two instruments on different candle schedules (B has a daily gap), pooled and time-sorted."""
    ta = np.arange(n) * 900
    tb = np.array([i * 900 for i in range(int(n * 1.4)) if (i // 96) % 4 != 3])[:n]

    def ends(t):
        idx = np.minimum(np.arange(len(t)) + horizon, len(t) - 1)
        return t[idx] + np.maximum(0, np.arange(len(t)) + horizon - (len(t) - 1)) * 900
    t, t_end = np.concatenate([ta, tb]), np.concatenate([ends(ta), ends(tb)])
    sym = np.concatenate([np.zeros(n, int), np.ones(len(tb), int)])
    o = np.lexsort((sym, t))
    return t[o], t_end[o], sym[o], horizon


def test_time_ordered_folds_keep_the_gap():
    t, t_end, sym, horizon = two_symbols()
    folds = setup.time_ordered_folds(t, t_end, n_splits=5, gap=horizon)
    assert len(folds) == 5
    sizes = [len(tr) for tr, _ in folds]
    assert sizes == sorted(sizes)                                   # expanding window
    for train_idx, cal_idx in folds:
        cal_start = t[cal_idx].min()
        assert t[train_idx].max() < cal_start                       # no train row is later than a calibration row
        assert t_end[train_idx].max() <= cal_start                  # no label window reaches into calibration
        assert not set(train_idx) & set(cal_idx)
        for s in (0, 1):                                            # at least `horizon` candles of each symbol
            own = np.flatnonzero(sym == s)
            last_train = max(i for i in own if i in set(train_idx))
            between = np.sum((t[own] > t[last_train]) & (t[own] < cal_start))
            assert between >= horizon - 1
    cals = np.concatenate([c for _, c in folds])
    assert len(cals) == len(set(cals))                              # calibration blocks do not overlap


def test_holdout_is_the_last_fifth_of_time_with_a_purge():
    t, t_end, _, _ = two_symbols()
    dev, hold, cut = split_by_time(t, t_end, 0.2)
    assert cut == pytest.approx(t.min() + 0.8 * (t.max() - t.min()), abs=1)
    assert t[hold].min() >= cut and t[dev].max() < cut and t_end[dev].max() <= cut
    assert (~dev & ~hold).sum() > 0                                 # the purged rows belong to neither side


# ----------------------------------------------------------------------------- loading

def req(**kw):
    return TrainRequest(**{"symbols": ["XAUUSD", "EURUSD"], "horizon": 8, **kw})


def test_npz_and_json_exports_load_the_same_rows(tmp_path):
    synth = SynthQuant(rows=400)
    q = QuantClient("http://quant.test", transport=synth.client_transport())
    store = Store(tmp_path)
    pips = {"XAUUSD": [0.1, 1.0]}
    a = load_setup_data(q, store, "15m", ["XAUUSD", "EURUSD"], pips, req(format="npz"))
    b = load_setup_data(q, store, "15m", ["XAUUSD", "EURUSD"], pips, req(format="json"))
    assert [s["key"] for s in a.segments] == ["XAUUSD@0.1", "XAUUSD@1", "EURUSD@0.0001"]
    assert a.X.dtype == np.float32 and a.X.shape == b.X.shape == (3 * (400 - 8), len(NAMES))
    np.testing.assert_array_equal(a.y, b.y)
    np.testing.assert_array_equal(a.t, b.t)
    np.testing.assert_array_equal(a.t_end, b.t_end)
    np.testing.assert_allclose(a.X, b.X, equal_nan=True)
    np.testing.assert_allclose(a.r_buy, b.r_buy, rtol=1e-6)
    np.testing.assert_allclose(a.labels, b.labels, equal_nan=True)
    assert a.label_names == ["cont_after_pullback"] and set(np.unique(a.y)) == {0, 1, 2}
    assert np.all(np.diff(a.t) >= 0)                                # pooled rows are in time order
    assert (tmp_path / "datasets").exists() and len(list((tmp_path / "datasets").glob("*.npz"))) == 3
    assert any(c[1].get("format") == "npz" for c in synth.calls)


def test_auto_falls_back_to_json_when_the_sidecar_has_no_npz(tmp_path):
    synth = SynthQuant(rows=400, npz=False)
    q = QuantClient("http://quant.test", transport=synth.client_transport())
    d = load_setup_data(q, Store(tmp_path), "15m", ["EURUSD"], {}, req())
    assert len(d.y) == 392


def test_a_damaged_export_is_fetched_again(tmp_path, monkeypatch):
    monkeypatch.setattr("rlcd.quant.time.sleep", lambda s: None)
    synth = SynthQuant(rows=400)
    synth.damage = 1                                                # right size, wrong bytes, once
    q = QuantClient("http://quant.test", transport=synth.client_transport())
    d = load_setup_data(q, Store(tmp_path), "15m", ["EURUSD"], {}, req(format="npz"))
    assert len(d.y) == 392 and sum(c[0] == "/dataset" for c in synth.calls) == 2
    synth.fail_500 = 1                                              # a collided export answers 500 once
    assert len(load_setup_data(q, Store(tmp_path), "15m", ["EURUSD"], {}, req(format="npz")).y) == 392
    synth.damage = 99                                               # never arrives intact: the head fails,
    c = make_client(tmp_path / "svc", synth)                        # the others are still committed
    job = train(c, {"heads": ["setup:15m", "intent"], "symbols": ["EURUSD"], "horizon": 8})
    assert job["status"] == "partial" and job["heads"]["intent"]["status"] == "trained"
    assert "arrived damaged" in job["heads"]["setup:15m"]["error"] and job["model"] == "rlcd-0.1.1"


def test_row_budget_takes_every_kth_candle_per_symbol(tmp_path):
    synth = SynthQuant(rows=1000)
    q = QuantClient("http://quant.test", transport=synth.client_transport())
    d = load_setup_data(q, Store(tmp_path), "15m", ["EURUSD", "GBPUSD"], {}, req(max_train_rows=1000))
    assert d.rows_available == 2 * 992 and d.stride == 2 and len(d.y) == 992
    for s in (0, 1):
        assert np.all(np.diff(d.t[d.seg == s]) == 2 * 900)          # every second candle, in order
    assert "kept every 2 candle" in d.notes[-1]


def test_unknown_symbols_are_skipped_not_fatal(tmp_path):
    synth = SynthQuant(rows=400)
    q = QuantClient("http://quant.test", transport=synth.client_transport())
    d = load_setup_data(q, Store(tmp_path), "15m", ["EURUSD", "DXY", "NOPE"], {}, req())
    assert [s["symbol"] for s in d.segments] == ["EURUSD"] and len(d.notes) == 2


# ----------------------------------------------------------------------------- a planted signal

def test_planted_signal_beats_the_base_rate_on_the_holdout(planted):
    m = planted.job["heads"]["setup:15m"]["metrics"]
    assert m["has_skill"] and m["brier"] < m["baseline_brier"] - 0.1 and m["log_loss"] < m["baseline_log_loss"]
    assert m["brier_skill_score"] > 0.2 and m["directional_auc"] > 0.9 and m["stride"] == 1
    cal = planted.get("/v1/calibration", params={"head": "setup:15m"}).json()
    assert cal["skill"]["lower_95"] > 0 and cal["policy"]["verdict"]["max_tier"] == "act"
    assert set(cal["by_segment"]) == {"XAUUSD@0.1", "XAUUSD@1", "EURUSD@0.0001", "GBPUSD@0.0001"}
    assert list(cal["by_year"]) == ["2025"] and cal["by_year"]["2025"]["n_holdout"] == cal["n_holdout"]
    assert all(v["has_skill"] for v in cal["by_segment"].values())
    one = planted.get("/v1/calibration", params={"head": "setup:15m", "symbol": "XAUUSD", "pip": 1.0}).json()
    assert one["scope"] == "holdout rows of XAUUSD@1" and len(one["reliability"]["top_label"]) == 10


def test_reliability_table_is_roughly_diagonal(planted):
    cal = planted.get("/v1/calibration", params={"head": "long_tp1:15m"}).json()
    assert cal["type"] == "noul" and cal["positive"] == "buy" and len(cal["reliability"]) == 10
    filled = [b for b in cal["reliability"] if b["count"] >= 60]
    assert len(filled) >= 6
    for b in filled:
        assert b["lo"] <= b["mean_predicted"] <= b["hi"]
        assert abs(b["mean_predicted"] - b["observed_rate"]) < 0.15
    rates = [b["observed_rate"] for b in filled]
    assert rates[-1] - rates[0] > 0.7                               # low bins rarely win, high bins mostly do
    assert cal["ece"] < 0.06 and cal["brier"] < cal["baseline"]["brier"]


def test_policy_on_the_holdout_beats_breakeven(planted):
    pol = planted.get("/v1/calibration", params={"head": "setup:15m"}).json()["policy"]
    act = pol["holdout"]["act"]
    assert act["n"] >= 30 and act["wilson_95"][0] > act["breakeven_rate"] and act["mean_r"] > 0
    assert act["wilson_95"][0] < act["win_rate"] < act["wilson_95"][1]
    assert pol["tuning"]["tuned"] and pol["config"]["tuned"]
    assert pol["tuning"]["validation"]["n"] >= pol["config"]["min_trades"]
    assert pol["bernoulli"]["act"]["p_value"] < 1e-6 and pol["bernoulli"]["act"]["trades_needed_80_power"] < 30


def test_decide_acts_on_a_strong_setup_with_fixed_pip_levels(planted):
    planted.synth.next_x = BULL
    r = planted.post("/v1/decide", json={"symbol": "XAUUSD", "interval": "15m", "text": "xauusd m15 where entry"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["action"] == "buy" and d["tier"] == "act" and d["calibrated"] is True
    assert d["probabilities"]["buy"] > 0.8 and sum(d["probabilities"].values()) == pytest.approx(1, abs=1e-4)
    assert d["price"] == 2400.0 and d["pip"] == 0.1 and d["entry"] == 2400.0
    assert d["stop"] == pytest.approx(2398.0) and d["targets"] == [pytest.approx(2405.0), pytest.approx(2410.0)]
    assert d["levels"]["sell"]["stop"] == pytest.approx(2402.0)
    assert d["breakeven_probability"] == pytest.approx((20 + 2.5) / 70, abs=1e-6)   # spread from the sidecar
    assert d["expected_r"]["buy"] == pytest.approx(d["probabilities"]["buy"] * 2.5
                                                   - (1 - d["probabilities"]["buy"]) - 0.125, abs=1e-5)
    assert d["thresholds"]["tuned"] and d["thresholds"]["min_p_above_breakeven"] == 0.9
    assert d["reasons"][0]["feature"] in ("tr_swing_trend", "ret4_atr") and d["reasons"][0]["contribution"] > 0
    assert d["reasons"][0]["timeframe"] in ("15m", None) and "raises P(buy)" in d["reasons"][0]["text"]
    assert d["trade_style"]["choice"] in ("daytrade", "swing") if d["trade_style"] else True
    assert d["calibration"]["has_skill"] and d["calibration"]["instrument"]["symbol"] == "XAUUSD"
    assert d["facts"]["alignment"] == {"up": 2} and d["warnings"] == []
    b = d["bernoulli"]
    assert b["p"] == d["probabilities"]["buy"] and b["payoff_b"] == 2.5 and b["breakeven"] == d["breakeven_probability"]
    assert b["kelly"] > 0.5 and b["kelly_quarter"] == 0.02 and b["stake_fraction"] == 0.02
    assert b["posterior"]["p_above_breakeven"] >= 0.9 and b["gate"] == {"threshold": 0.9, "mode": "posterior",
                                                                       "passed": True}
    assert b["posterior"]["alpha"] == b["posterior"]["wins"] + 1


def test_group_shares_sum_to_one(planted):
    planted.synth.next_x = BULL
    d = planted.post("/v1/decide", json={"symbol": "EURUSD", "interval": "15m"}).json()
    g = d["reason_groups"]
    assert sum(g.values()) == pytest.approx(1.0, abs=1e-3) and set(g) <= {"structure", "strength", "volatility",
                                                                           "session", "candle"}
    assert g["structure"] + g["strength"] > 0.9                     # the planted features carry the call
    assert list(g.values()) == sorted(g.values(), reverse=True)


def test_small_stop_warning(planted):
    planted.synth.next_x = [0.0, 0.0, 0.0, 0.3, 30.0, 0.0, 0.0, 0.0]
    d = planted.post("/v1/decide", json={"symbol": "EURUSD", "interval": "15m"}).json()
    assert any("smaller than half a candle" in w and "0.30 ATR" in w for w in d["warnings"])
    planted.synth.next_x = [0.0, 0.0, 0.0, 0.9, 30.0, 0.0, 0.0, 0.0]
    d = planted.post("/v1/decide", json={"symbol": "EURUSD", "interval": "15m"}).json()
    assert not any("smaller than half a candle" in w for w in d["warnings"])


def test_decide_holds_for_other_stop_and_target(planted):
    planted.synth.next_x = BULL
    d = planted.post("/v1/decide", json={"symbol": "EURUSD", "interval": "15m", "sl_pips": 10, "tp_pips": 30}).json()
    assert d["action"] == "hold" and any("trained for stop 20 / target 50" in w for w in d["warnings"])


def test_unseen_instrument_is_capped_at_confirm(planted):
    planted.synth.next_x = BULL
    d = planted.post("/v1/decide", json={"symbol": "XAUUSD", "interval": "15m", "pip": 0.5}).json()
    assert d["tier"] == "confirm" and d["action"] == "buy" and d["max_tier"] == "confirm"
    assert any("was not in the training set" in w for w in d["warnings"])


def test_feature_version_mismatch_is_422(planted):
    x = [0.1] * len(NAMES)
    ok = planted.post("/v1/systemone", json={
        "state": {"x": x, "feature_names": NAMES, "feature_version": "synth-v1"},
        "questions": {"setup:15m": {"type": "choice", "criteria": {"buy": "", "sell": "", "hold": ""}},
                      "long": {"type": "noul", "head": "long_tp1:15m"},
                      "short": {"type": "noul", "head": "short_tp1:15m"}}})
    assert ok.status_code == 200, ok.text
    a = ok.json()["answers"]
    assert a["long"]["noul"] == a["setup:15m"]["probabilities"]["buy"]
    assert a["short"]["noul"] == a["setup:15m"]["probabilities"]["sell"]
    assert a["long"]["confidence"] == pytest.approx(abs(2 * a["long"]["noul"] - 1), abs=1e-5)
    bad = planted.post("/v1/systemone", json={
        "state": {"x": x, "feature_names": NAMES, "feature_version": "synth-v2"},
        "questions": {"setup:15m": {"type": "choice"}}})
    assert bad.status_code == 422 and bad.json()["kind"] == "feature_version_mismatch"
    assert bad.json()["expected"] == "synth-v1" and "synth-v2" in bad.json()["error"]
    short = planted.post("/v1/systemone", json={"state": {"x": x[:-1], "feature_version": "synth-v1"},
                                                "questions": {"setup:15m": {"type": "choice"}}})
    assert short.status_code == 422 and short.json()["kind"] == "bad_feature_vector"
    planted.synth.feature_version = "synth-v2"                      # the sidecar moved on, the model did not
    try:
        r = planted.post("/v1/decide", json={"symbol": "EURUSD", "interval": "15m"})
        assert r.status_code == 422 and r.json()["kind"] == "feature_version_mismatch"
    finally:
        planted.synth.feature_version = "synth-v1"


def test_missing_values_are_scored(planted):
    x = [None] * len(NAMES)
    r = planted.post("/v1/systemone", json={"state": {"x": x, "feature_version": "synth-v1"},
                                            "questions": {"setup:15m": {"type": "choice"}}})
    assert r.status_code == 200 and sum(r.json()["answers"]["setup:15m"]["probabilities"].values()) == \
        pytest.approx(1, abs=1e-4)


def test_rank_by_noul(planted):
    xs = {"bull": BULL, "bear": [-2.5, -1.0, -2.5, 1.2, 30.0, 0.0, 0.0, 0.0], "flat": [0.0] * 8}
    cands = [{"id": k, "state": {"x": v}} for k, v in xs.items()]
    r = planted.post("/v1/rank", json={"state": {"feature_version": "synth-v1"}, "candidates": cands,
                                       "question": {"type": "noul", "head": "long_tp1:15m"}})
    assert r.status_code == 200, r.text
    assert [c["id"] for c in r.json()["candidates"]] == ["bull", "flat", "bear"]


# ----------------------------------------------------------------------------- pattern nouls, composite, scan

def test_pattern_head_is_discovered_and_only_answers_on_pattern(planted):
    m = planted.job["heads"]["cont_after_pullback:15m"]
    assert m["status"] == "trained" and m["metrics"]["has_skill"] and m["metrics"]["applicability_gate_accuracy"] > 0.99
    assert 0.3 < m["metrics"]["applicable_share"] < 0.5 and m["metrics"]["log_loss"] < m["metrics"]["baseline_log_loss"]
    heads = {h["name"]: h for h in planted.get("/v1/heads").json()["heads"]}
    assert heads["cont_after_pullback:15m"]["type"] == "noul" and heads["cont_after_pullback:15m"]["kind"] == "pattern"
    q = {"q": {"type": "noul", "head": "cont_after_pullback:15m"}}
    on = planted.post("/v1/systemone", json={"state": {"x": BULL, "feature_version": "synth-v1"}, "questions": q})
    a = on.json()["answers"]["q"]
    assert a["type"] == "noul" and a["applicable"] is True and a["noul"] > 0.6 and a["calibrated"] is True
    assert a["confidence"] == pytest.approx(abs(2 * a["noul"] - 1), abs=1e-5)
    off = planted.post("/v1/systemone", json={"state": {"x": BULL_NO_PATTERN, "feature_version": "synth-v1"},
                                              "questions": q})
    assert off.json()["answers"]["q"] == {"type": "noul", "noul": None, "confidence": None,
                                          "head": "cont_after_pullback:15m", "calibrated": True, "applicable": False}
    # what the sidecar declares wins over the learned gate
    said = planted.post("/v1/systemone", json={
        "state": {"x": BULL, "feature_version": "synth-v1", "applicable": {"cont_after_pullback": False}},
        "questions": q})
    assert said.json()["answers"]["q"]["applicable"] is False
    cal = planted.get("/v1/calibration", params={"head": "cont_after_pullback:15m"}).json()
    assert len(cal["reliability"]) == 10 and cal["by_year"]["2025"]["n"] == cal["n_holdout"]


def test_decide_reports_nouls_and_a_composite_with_visible_weights(planted):
    planted.synth.next_x = BULL
    d = planted.post("/v1/decide", json={"symbol": "EURUSD", "interval": "15m"}).json()
    assert set(d["nouls"]) == {"long_tp1", "short_tp1", "cont_after_pullback"} and d["not_applicable"] == []
    c = d["composite"]
    assert c["weights"] == {"tp1": pytest.approx(0.4 / 0.55, abs=1e-4),
                            "cont_after_pullback": pytest.approx(0.15 / 0.55, abs=1e-4)}
    want = c["weights"]["tp1"] * d["nouls"]["long_tp1"]["noul"] + \
        c["weights"]["cont_after_pullback"] * d["nouls"]["cont_after_pullback"]["noul"]
    assert c["buy"] == pytest.approx(want, abs=1e-4) and c["buy"] > c["sell"]
    assert c["configured_weights"] == P.CompositeConfig().weights
    planted.synth.next_x = BULL_NO_PATTERN
    d = planted.post("/v1/decide", json={"symbol": "EURUSD", "interval": "15m"}).json()
    assert d["not_applicable"] == ["cont_after_pullback"] and set(d["nouls"]) == {"long_tp1", "short_tp1"}
    assert d["composite"]["weights"] == {"tp1": 1.0}


def test_composite_arithmetic():
    out = P.composite({"long_tp1": 0.5, "short_tp1": 0.2, "fakeout": 0.8, "cont_after_pullback": 0.7,
                       "mystery": 0.9}, {"fakeout": 1, "cont_after_pullback": -1})
    # a likely fakeout of an upward break argues for the sell side; a down-pullback continuation too
    assert out["weights"] == {"tp1": pytest.approx(0.4 / 0.65, abs=1e-4),
                              "cont_after_pullback": pytest.approx(0.15 / 0.65, abs=1e-4),
                              "fakeout": pytest.approx(0.1 / 0.65, abs=1e-4)}
    assert out["sell"] == pytest.approx((0.4 * 0.2 + 0.15 * 0.7 + 0.1 * 0.8) / 0.65, abs=1e-4)
    assert out["buy"] == pytest.approx((0.4 * 0.5 + 0.15 * 0.3 + 0.1 * 0.2) / 0.65, abs=1e-4)
    assert out["unscored"] == ["mystery"]


def test_scan_orders_by_expected_r(planted):
    planted.synth.next_x = None                                     # every symbol's own last candle
    r = planted.post("/v1/scan", json={"interval": "15m"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert [x["symbol"] for x in out["results"]] and {x["symbol"] for x in out["results"]} == \
        {"XAUUSD", "EURUSD", "GBPUSD"}                               # gold + the majors the sidecar offers
    keys = [(x["best_expected_r"], x["p_above_breakeven"]) for x in out["results"]]
    assert keys == sorted(keys, reverse=True) and [x["rank"] for x in out["results"]] == [1, 2, 3]
    for x in out["results"]:
        assert x["best_expected_r"] == max(x["decision"]["expected_r"].values())
        assert x["session"] == x["decision"]["session"] and x["decision"]["decision_id"]
    some = planted.post("/v1/scan", json={"symbols": ["eurusd", "DXY"], "pip": {"EURUSD": 0.0001}}).json()
    assert [x["symbol"] for x in some["results"]] == ["EURUSD"] and some["errors"][0]["symbol"] == "DXY"


# ----------------------------------------------------------------------------- the reinforcement loop

def test_feedback_updates_the_posterior_now_and_trains_later(tmp_path):
    c = make_client(tmp_path, SynthQuant(signal=2.0, rows=1500))
    body = {"heads": ["setup:15m"], "symbols": ["EURUSD", "GBPUSD"], "horizon": 8, "n_estimators": 40,
            "patterns": False}
    job = train(c, body)
    assert job["status"] == "succeeded" and job["heads"]["setup:15m"]["n_feedback"] == 0
    assert list(job["heads"]) == ["setup:15m"]
    c.synth.next_x = BULL
    d = c.post("/v1/decide", json={"symbol": "EURUSD", "interval": "15m"}).json()
    before = d["bernoulli"]["posterior"]
    fb = c.post("/v1/feedback", json={"decision_id": d["decision_id"], "label": "buy", "source": "view-engine"})
    assert fb.status_code == 200 and fb.json()["bernoulli"]["updated"] is True
    again = c.post("/v1/feedback", json={"decision_id": d["decision_id"], "label": "buy", "source": "view-engine"})
    assert again.json()["bernoulli"] == {"updated": False, "reason": "this decision_id was already scored"}
    after = c.post("/v1/decide", json={"symbol": "EURUSD", "interval": "15m"}).json()["bernoulli"]["posterior"]
    assert after["wins"] == before["wins"] + 1 and after["losses"] == before["losses"]      # no retrain needed
    assert after["alpha"] == before["alpha"] + 1 and after["mean"] >= before["mean"]
    table = c.get("/v1/bernoulli", params={"head": "setup:15m"}).json()
    assert table["online_trials"] == 1 and len(table["bins"]) == 20 and table["binomial_test"]["act"]["n"] > 0
    assert sum(b["wins"] + b["losses"] for b in table["bins"] if b["action"] == "buy") == table["holdout_trials"] + 1
    assert c.post("/v1/feedback", json={"decision_id": "nope", "label": "buy", "source": "t"}).status_code == 422
    assert c.post("/v1/feedback", json={"head": "long_tp1:15m", "x": BULL, "feature_version": "synth-v1",
                                        "label": "buy", "source": "t"}).status_code == 422
    raw = c.post("/v1/feedback", json={"head": "setup:15m", "x": BULL, "feature_version": "synth-v1",
                                       "label": "sell", "source": "manual"})
    assert raw.status_code == 200 and raw.json()["bernoulli"] is None and raw.json()["feedback_rows"] == 3
    job2 = train(c, body)
    assert job2["heads"]["setup:15m"]["n_feedback"] == 3            # picked up by the next train
    assert job2["heads"]["setup:15m"]["n_train"] == job["heads"]["setup:15m"]["n_train"] + 3
    assert job2["model"] == "rlcd-0.1.2"
    fresh = c.get("/v1/bernoulli", params={"head": "setup:15m"}).json()
    assert fresh["online_trials"] == 0 and fresh["model_version"] == "rlcd-0.1.2"


def test_exploration_is_off_by_default_and_labelled_when_on(planted):
    planted.synth.next_x = BULL
    d = planted.post("/v1/decide", json={"symbol": "EURUSD", "interval": "15m"}).json()
    assert d["exploration"] is False and d["bernoulli"]["gate"]["mode"] == "posterior"
    e = planted.post("/v1/decide", json={"symbol": "EURUSD", "interval": "15m", "explore": True}).json()
    assert e["exploration"] is True and e["bernoulli"]["gate"]["mode"] == "thompson_sampling"
    assert 0 < e["bernoulli"]["gate"]["sampled_win_rate"] < 1 and any("exploration mode" in w for w in e["warnings"])


def test_gate_turns_act_into_confirm_without_evidence(planted):
    """Same strong prediction, but the bin has no outcomes behind it: a uniform prior puts only 68% of its
    mass above breakeven, so the decision is confirm, not act."""
    led = planted.app.state.svc.store.ledger
    saved = led.table("setup:15m")
    try:
        led.reset("setup:15m", {**led.empty(saved["model_version"])})
        planted.synth.next_x = BULL
        d = planted.post("/v1/decide", json={"symbol": "EURUSD", "interval": "15m"}).json()
        assert d["tier"] == "confirm" and d["action"] == "buy" and d["bernoulli"]["stake_fraction"] == 0.0
        assert d["bernoulli"]["gate"]["passed"] is False and d["bernoulli"]["posterior"]["n"] == 0
        assert any("Bernoulli gate" in w for w in d["warnings"])
    finally:
        led.reset("setup:15m", saved)


# ----------------------------------------------------------------------------- pure noise

def test_noise_shows_no_skill_and_the_policy_holds(noise):
    m = noise.job["heads"]["setup:15m"]["metrics"]
    assert m["has_skill"] is False and abs(m["brier_skill_score"]) < 0.02
    assert m["policy"]["max_tier"] == "hold" and "no skill" in m["policy"]["reason"]
    assert 0.4 < m["directional_auc"] < 0.6
    for x in (BULL, [0.0] * 8, None):
        noise.synth.next_x = x
        d = noise.post("/v1/decide", json={"symbol": "XAUUSD", "interval": "15m"}).json()
        assert d["action"] == "hold" and d["tier"] == "hold" and d["stop"] is None and d["targets"] == []
        assert any("no skill over the base rate" in w for w in d["warnings"])
        assert d["bernoulli"]["stake_fraction"] == 0.0
        assert max(d["probabilities"].values()) < 0.6               # calibrated noise stays near the base rate
    r = noise.post("/v1/systemone", json={"state": {"symbol": "EURUSD"},
                                          "questions": {"setup:15m": {"type": "choice"}}}).json()
    assert any("no skill" in w for w in r["warnings"]) and r["answers"]["setup:15m"]["confidence"] < 0.3


def test_untrained_decision_is_hold_with_a_warning(tmp_path):
    c = make_client(tmp_path, SynthQuant(rows=300))
    d = c.post("/v1/decide", json={"symbol": "XAUUSD", "interval": "1h"}).json()
    assert d["action"] == "hold" and d["calibrated"] is False and d["calibration"] is None
    assert d["probabilities"] == {"buy": pytest.approx(1 / 3, abs=1e-5), "sell": pytest.approx(1 / 3, abs=1e-5),
                                  "hold": pytest.approx(1 / 3, abs=1e-5)}
    assert "not trained" in d["warnings"][0] and d["levels"]["buy"]["stop"] == pytest.approx(2398.0)


def test_train_validation_and_failures(client):
    assert client.post("/v1/train", json={"heads": ["nope"]}).status_code == 422
    assert client.post("/v1/train", json={"intervals": ["2h"]}).status_code == 422
    assert client.post("/v1/train", json={"heads": ["setup:15m"],
                                          "quant_url": "http://example.com"}).status_code == 422
    assert client.get("/v1/train/missing").status_code == 404
    job = train(client, {"heads": ["setup:15m", "intent"]})         # the sidecar is down: one head fails
    assert job["status"] == "partial" and job["heads"]["intent"]["status"] == "trained"
    assert job["heads"]["setup:15m"]["status"] == "failed" and "unreachable" in job["heads"]["setup:15m"]["error"]
    assert job["progress"]["done"] == job["progress"]["total"] == 2


def test_verdict_levels():
    cfg = P.PolicyConfig()

    def stats(n, wins, mean_r, be=0.3):
        from rlcd.metrics import wilson
        lo, hi = wilson(wins, n)
        return {"n": n, "wins": wins, "win_rate": wins / n if n else None, "wilson_95": [lo, hi],
                "breakeven_rate": be, "mean_r": mean_r}
    none = stats(0, 0, None)
    assert P.verify(False, {"act": stats(200, 150, 1.5), "act_or_confirm": stats(200, 150, 1.5)}, cfg)["max_tier"] == "hold"
    # skill on the Brier score but signals at breakeven: 30 of 96 against 0.299 is nothing
    coin = P.verify(True, {"act": none, "act_or_confirm": stats(96, 30, 0.047, 0.299)}, cfg)
    assert coin["max_tier"] == "hold" and "not distinguishable from breakeven" in coin["reason"]
    assert coin["p_value"] > 0.3
    assert P.verify(True, {"act": none, "act_or_confirm": stats(10, 9, 2.0)}, cfg)["max_tier"] == "hold"
    assert P.verify(True, {"act": none, "act_or_confirm": stats(100, 45, -0.1)}, cfg)["max_tier"] == "hold"
    assert P.verify(True, {"act": stats(10, 8, 1.8), "act_or_confirm": stats(100, 40, 0.4)}, cfg)["max_tier"] == "confirm"
    assert P.verify(True, {"act": stats(60, 22, 0.2), "act_or_confirm": stats(100, 40, 0.4)}, cfg)["max_tier"] == "confirm"
    assert P.verify(True, {"act": stats(60, 40, 1.3), "act_or_confirm": stats(100, 50, 0.7)}, cfg)["max_tier"] == "act"
    # one instrument: thin data is a cap at confirm, not a veto
    assert P.verify(True, {"act": none, "act_or_confirm": stats(5, 2, 0.4)}, cfg, pooled=False)["max_tier"] == "confirm"
    assert P.verify(True, {"act": none, "act_or_confirm": stats(40, 8, -0.3)}, cfg, pooled=False)["max_tier"] == "hold"
