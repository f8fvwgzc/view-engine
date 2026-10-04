"""Gradient-boosted direction classifier + quantile regressors with walk-forward validation.

LightGBM is the engine; if it cannot be imported (e.g. missing libomp on macOS) we fall back to
scikit-learn's HistGradientBoosting* (same family of model) and report `engine` accordingly.
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

from . import indicators as ind
from .cache import TTL_MODEL, cache
from .data import bar_hours, get_series, iso, norm_interval
from .symbols import REGISTRY, Symbol

log = logging.getLogger("quant.model")

try:  # pragma: no cover - environment dependent
    import lightgbm as lgb
    ENGINE = "lightgbm"
except Exception as e:  # pragma: no cover
    lgb = None
    ENGINE = "sklearn-hgb"
    log.warning("lightgbm unavailable (%s); using sklearn HistGradientBoosting fallback", e)

MIN_SAMPLES = 250
N_FOLDS = 5
QUANTILES = (0.1, 0.5, 0.9)
MAX_TRAIN_ROWS = 6000
MAX_RELATED = 5
CAVEAT = ("Statistical estimate from historical price patterns only; not financial advice. Short-horizon "
          "direction is close to a coin flip for liquid markets — compare accuracy with the baseline and "
          "treat prob_up near 0.5 as no signal. Does not know about scheduled news/events.")


# ------------------------------------------------------------------ horizon parsing

def parse_horizon(horizon: str | int | None, interval: Optional[str]) -> tuple[str, int]:
    """'1' (+interval) -> bars; '4h' -> (1h, 4); '1d' -> (1d, 1); '1w' -> (1d, 5)."""
    h = str(horizon if horizon is not None else "1").strip().lower()
    if re.fullmatch(r"\d+", h):
        return norm_interval(interval or "1d"), max(1, int(h))
    m = re.fullmatch(r"(\d+)\s*(m|min|h|hr|d|day|w|wk|week)s?", h)
    if not m:
        raise ValueError(f"bad horizon '{horizon}' (use bars like 1 or durations like 4h, 1d, 1w)")
    n, u = int(m.group(1)), m.group(2)
    if u in ("m", "min"):
        iv = norm_interval(interval) if interval and interval in ("5m", "15m", "30m") else "15m"
        return iv, max(1, round(n / (ind_minutes(iv))))
    if u in ("h", "hr"):
        if interval in ("1h", "4h") and n % (4 if interval == "4h" else 1) == 0:
            return interval, n // (4 if interval == "4h" else 1)
        return "1h", n
    if u in ("d", "day"):
        return "1d", n
    return "1d", 5 * n


def ind_minutes(iv: str) -> float:
    return bar_hours(iv) * 60


# ------------------------------------------------------------------ features

def build_features(df: pd.DataFrame, related: dict[str, pd.Series], intraday: bool) -> pd.DataFrame:
    c = df["c"]
    lr = np.log(c).diff()
    f = pd.DataFrame(index=df.index)
    for k in (1, 2, 3, 5, 10, 20):
        f[f"ret_{k}"] = np.log(c / c.shift(k))
    f["rv_20"] = lr.rolling(20).std()
    f["rv_ratio_5_20"] = lr.rolling(5).std() / f["rv_20"]
    f["atr_pct"] = ind.atr(df, 14) / c
    f["rsi_14"] = ind.rsi(c, 14)
    for n in (20, 50, 200):
        f[f"dist_sma{n}"] = c / ind.sma(c, n) - 1
    f["range_pos_20"] = (c - df["l"].rolling(20).min()) / (df["h"].rolling(20).max() - df["l"].rolling(20).min())
    f["dow"] = df.index.dayofweek
    if intraday:
        f["hour"] = df.index.hour
    for name, s in related.items():
        s = s.reindex(df.index.union(s.index)).ffill().reindex(df.index)
        r = np.log(s).diff() if (s.dropna() > 0).all() else s.diff()
        # lagged one bar so differing close times can't leak the future
        f[f"{name}_ret1_lag1"] = r.shift(1)
        f[f"{name}_ret5_lag1"] = r.rolling(5).sum().shift(1)
    return f.replace([np.inf, -np.inf], np.nan)


def make_target(c: pd.Series, h: int) -> pd.Series:
    return np.log(c.shift(-h) / c)


# ------------------------------------------------------------------ walk-forward

def walk_forward_splits(n: int, n_folds: int = N_FOLDS, min_train_frac: float = 0.5,
                        gap: int = 0) -> list[tuple[int, int, int]]:
    """Expanding-window splits -> [(train_end, test_start, test_end)] (python slice bounds).
    Test region = last (1-min_train_frac) of rows split into n_folds consecutive blocks.
    `gap` rows are purged between train and test (use horizon h to avoid overlapping labels)."""
    start = int(n * min_train_frac)
    if n - start < n_folds:
        return []
    edges = np.linspace(start, n, n_folds + 1).astype(int)
    out = []
    for i in range(n_folds):
        ts, te = int(edges[i]), int(edges[i + 1])
        tr_end = ts - gap
        if tr_end <= 0 or te <= ts:
            continue
        out.append((tr_end, ts, te))
    return out


def _clf():
    if lgb is not None:
        return lgb.LGBMClassifier(n_estimators=200, learning_rate=0.03, num_leaves=15, min_child_samples=40,
                                  subsample=0.8, subsample_freq=1, colsample_bytree=0.8, reg_lambda=1.0,
                                  n_jobs=2, verbose=-1)
    from sklearn.ensemble import HistGradientBoostingClassifier
    return HistGradientBoostingClassifier(max_iter=200, learning_rate=0.03, max_leaf_nodes=15, min_samples_leaf=40)


def _qreg(alpha: float):
    if lgb is not None:
        return lgb.LGBMRegressor(objective="quantile", alpha=alpha, n_estimators=200, learning_rate=0.03,
                                 num_leaves=15, min_child_samples=40, subsample=0.8, subsample_freq=1,
                                 colsample_bytree=0.8, n_jobs=2, verbose=-1)
    from sklearn.ensemble import HistGradientBoostingRegressor
    return HistGradientBoostingRegressor(loss="quantile", quantile=alpha, max_iter=200, learning_rate=0.03,
                                         max_leaf_nodes=15, min_samples_leaf=40)


def validate(X: pd.DataFrame, y: np.ndarray, h: int) -> dict:
    splits = walk_forward_splits(len(X), N_FOLDS, 0.5, gap=h)
    probs, ys, base_preds, base_probs, folds = [], [], [], [], []
    for tr_end, ts, te in splits:
        Xtr, ytr = X.iloc[:tr_end], y[:tr_end]
        if len(np.unique(ytr)) < 2:
            continue
        m = _clf().fit(Xtr, ytr)
        p = m.predict_proba(X.iloc[ts:te])[:, 1]
        yt = y[ts:te]
        rate = float(ytr.mean())
        maj = 1 if rate >= 0.5 else 0
        probs.append(p); ys.append(yt)
        base_preds.append(np.full(len(yt), maj)); base_probs.append(np.full(len(yt), rate))
        folds.append({"train_rows": tr_end, "test_rows": te - ts,
                      "accuracy": float(((p >= 0.5).astype(int) == yt).mean()),
                      "baseline_accuracy": float((maj == yt).mean())})
    if not folds:
        return {"status": "insufficient_data", "n_folds": 0}
    p, yt = np.concatenate(probs), np.concatenate(ys)
    bp, bpr = np.concatenate(base_preds), np.concatenate(base_probs)
    acc = float(((p >= 0.5).astype(int) == yt).mean())
    base = float((bp == yt).mean())
    return {
        "scheme": f"expanding window, {len(folds)} folds over last 50% of samples, {h}-bar purge gap",
        "n_folds": len(folds), "n_oos": int(len(yt)),
        "accuracy": acc, "baseline_accuracy": base, "edge_vs_baseline": acc - base,
        "brier": float(np.mean((p - yt) ** 2)), "baseline_brier": float(np.mean((bpr - yt) ** 2)),
        "oos_up_rate": float(yt.mean()), "folds": folds,
        "brier_skill_score": float(1 - np.mean((p - yt) ** 2) / max(np.mean((bpr - yt) ** 2), 1e-12)),
        "_oos": (p, yt),
    }


def fit_calibrator(p: np.ndarray, y: np.ndarray):
    """Platt scaling of OOS walk-forward probabilities (logit(p) -> y). With no real skill the slope
    shrinks toward 0 and calibrated probabilities collapse toward the base rate."""
    from sklearn.linear_model import LogisticRegression
    if len(np.unique(y)) < 2 or len(y) < 50:
        return None
    z = np.log(np.clip(p, 1e-4, 1 - 1e-4) / (1 - np.clip(p, 1e-4, 1 - 1e-4))).reshape(-1, 1)
    return LogisticRegression(C=1.0).fit(z, y)


def apply_calibrator(cal, p: float) -> float:
    if cal is None:
        return p
    pc = float(np.clip(p, 1e-4, 1 - 1e-4))
    return float(cal.predict_proba(np.array([[np.log(pc / (1 - pc))]]))[:, 1][0])


# ------------------------------------------------------------------ training / prediction

@dataclass
class Trained:
    clf: object
    qregs: dict
    features: list[str]
    validation: dict
    n_samples: int
    trained_at: datetime
    top_features: list[dict]
    related_used: list[str]
    calibrator: object = None


def _load_related(sym: Symbol, interval: str) -> dict[str, pd.Series]:
    out = {}
    rel = [r for r in sym.related if r in REGISTRY][:MAX_RELATED]

    def load(rid):
        return rid, get_series(REGISTRY[rid], interval).df["c"]
    with ThreadPoolExecutor(max_workers=5) as ex:
        for fut in [ex.submit(load, r) for r in rel]:
            try:
                rid, s = fut.result()
                out[rid] = s
            except Exception as e:
                log.info("related series failed: %s", e)
    return out


def _dataset(sym: Symbol, interval: str, h: int):
    series = get_series(sym, interval)
    df = series.df
    related = _load_related(sym, interval)
    # drop related series that cover too little of the target's history (e.g. sparse intraday yields)
    related = {k: v for k, v in related.items() if v.index.min() <= df.index[min(len(df) - 1, 250)]}
    intraday = bar_hours(interval) < 24
    X = build_features(df, related, intraday)
    fwd = make_target(df["c"], h)
    return series, X, fwd, list(related)


def _train(sym: Symbol, interval: str, h: int) -> Trained | dict:
    series, X, fwd, related_used = _dataset(sym, interval, h)
    # trees handle NaN natively; only require the target and the core return features
    lab = X.join(fwd.rename("y")).dropna(subset=["y", "ret_20", "rv_20", "atr_pct"])
    lab = lab.iloc[-MAX_TRAIN_ROWS:]
    if len(lab) < MIN_SAMPLES:
        return {"status": "insufficient_data", "n_samples": int(len(lab)), "min_required": MIN_SAMPLES}
    feats = [c for c in X.columns]
    Xl, r = lab[feats], lab["y"].values
    y = (r > 0).astype(int)
    if len(np.unique(y)) < 2:
        return {"status": "insufficient_data", "n_samples": int(len(lab)), "reason": "single class"}
    val = validate(Xl, y, h)
    oos = val.pop("_oos", None)
    cal = fit_calibrator(*oos) if oos else None
    clf = _clf().fit(Xl, y)
    qregs = {q: _qreg(q).fit(Xl, r) for q in QUANTILES}
    imp = getattr(clf, "feature_importances_", None)
    if imp is None:
        top = []
    else:
        imp = np.asarray(imp, float)
        imp = imp / imp.sum() if imp.sum() > 0 else imp
        order = np.argsort(imp)[::-1][:8]
        top = [{"feature": feats[i], "importance": round(float(imp[i]), 4)} for i in order]
    return Trained(clf, qregs, feats, val, int(len(lab)), datetime.now(timezone.utc), top, related_used, cal)


def predict(sym: Symbol, interval: str = "1d", horizon: str | int | None = 1) -> dict:
    interval, h = parse_horizon(horizon, interval)
    key = ("model", sym.id, interval, h, ENGINE)
    trained = cache.get_or_set(key, TTL_MODEL, lambda: _train(sym, interval, h))
    base = {"symbol": sym.id, "interval": interval, "horizon_bars": h,
            "horizon_hours": h * bar_hours(interval), "engine": ENGINE, "caveat": CAVEAT}
    if isinstance(trained, dict):
        series = get_series(sym, interval)
        return {**base, **trained, "source": series.source, "as_of": series.as_of}
    # fresh features for the latest bar (model may be up to 6h old)
    series, X, _, _ = _dataset(sym, interval, h)
    for col in trained.features:
        if col not in X:
            X[col] = np.nan
    x_last = X[trained.features].iloc[[-1]]
    prob_raw = float(trained.clf.predict_proba(x_last)[:, 1][0])
    prob = apply_calibrator(trained.calibrator, prob_raw)
    v = trained.validation
    has_edge = (v.get("edge_vs_baseline") or 0) > 0.01 and (v.get("brier_skill_score") or 0) > 0
    qs = sorted(float(trained.qregs[q].predict(x_last)[0]) for q in QUANTILES)
    last = float(series.df["c"].iloc[-1])
    is_rate = sym.asset_class == "rate"
    return {
        **base, "status": "ok",
        "last": last, "last_bar_time": series.as_of,
        "prob_up": prob, "prob_up_raw": prob_raw,
        "prob_note": "prob_up is Platt-calibrated on walk-forward out-of-sample predictions (shrinks toward the "
                     "base rate when the model has no skill); prob_up_raw is the uncalibrated classifier output.",
        "signal_quality": "model beat baseline out-of-sample" if has_edge else
                          "NO demonstrated out-of-sample edge — treat as ~base rate / no signal",
        "expected_return_quantiles": {f"q{int(q * 100)}": float(np.expm1(v)) for q, v in zip(QUANTILES, qs)},
        "expected_price_range": {f"q{int(q * 100)}": float(last * np.exp(v)) for q, v in zip(QUANTILES, qs)},
        "validation": {**trained.validation, "n_samples": trained.n_samples},
        "top_features": trained.top_features, "related_features_from": trained.related_used,
        "trained_at": iso(trained.trained_at),
        "source": series.source, "as_of": series.as_of, "delayed_minutes": series.delayed_minutes,
        **({"note": "rate symbol: returns are log changes of the yield level"} if is_rate else {}),
    }


# ================================================================== structure-event model
# Label: after a BOS / box-breakout CLOSE, does price reach +1R in the signal direction before the
# protected-swing stop (body extreme ± 0.2 ATR) within K candles? Same-candle touch of both = loss.

STRUCT_K = 24
STRUCT_MIN_EVENTS = 120
STRUCT_TYPES = ("bos_up", "bos_down", "box_breakout_up", "box_breakout_down")


def _session_flags(ts) -> dict:
    from .structure import session_tag
    st = session_tag(ts)
    s = set(st["sessions"])
    return {"sess_sydney": int("Sydney" in s), "sess_tokyo": int("Tokyo" in s), "sess_london": int("London" in s),
            "sess_newyork": int("New York" in s),
            "trans_code": {"Tokyo→London": 1, "London→New York": 2, "New York close": 3}.get(st["transition"], 0)}


def structure_events(ctx) -> list[dict]:
    """All historical BOS + box-breakout events (causal) with entry/stop for labelling."""
    from .structure import bodies, box_at
    df = ctx.df
    c = df["c"].to_numpy(float)
    b = bodies(df)
    evs = []
    for e in ctx.run.events:
        if e.type in ("bos_up", "bos_down"):
            evs.append({"t": e.idx, "type": e.type, "dir": 1 if e.type == "bos_up" else -1, "level": e.level,
                        "box": None})
    for t in range(2, len(df)):
        prior = box_at(df, t - 1, _b=b)
        if prior and (c[t] > prior["high"] or c[t] < prior["low"]):
            up = c[t] > prior["high"]
            evs.append({"t": t, "type": "box_breakout_up" if up else "box_breakout_down", "dir": 1 if up else -1,
                        "level": prior["high"] if up else prior["low"], "box": prior})
    evs.sort(key=lambda x: (x["t"], x["type"]))
    return evs


def _protected(ctx, t: int, direction: int, entry: float):
    import bisect
    conf = getattr(ctx, "_conf", None)
    if conf is None:
        conf = ctx._conf = [s.confirmed_idx for s in ctx.run.swings]
    hi = bisect.bisect_right(conf, t)
    for s in reversed(ctx.run.swings[:hi]):
        if direction > 0 and s.kind == "L" and s.price < entry:
            return s
        if direction < 0 and s.kind == "H" and s.price > entry:
            return s
    return None


def event_features(ctx, ev: dict) -> Optional[dict]:
    from .structure import STOP_BUFFER_ATR
    t, d = ev["t"], ev["dir"]
    df = ctx.df
    atr = ctx.atr[t]
    if not np.isfinite(atr) or atr <= 0:
        return None
    entry = float(df["c"].iloc[t])
    prot = _protected(ctx, t, d, entry)
    if prot is None:
        return None
    stop = prot.price - d * STOP_BUFFER_ATR * atr
    risk = (entry - stop) * d
    if risk <= 0 or risk > 10 * atr:
        return None
    o = float(df["o"].iloc[t])
    sw = getattr(ctx, "_sweep_idx", None)
    if sw is None:
        sw = {k: np.array([e.idx for e in ctx.run.events if e.type == k]) for k in ("sweep_low", "sweep_high")}
        ctx._sweep_idx = sw

    def _count(kind):
        a = sw[kind]
        return int(np.searchsorted(a, t, side="right") - np.searchsorted(a, t - 5, side="left"))
    sweeps_opp = _count("sweep_low" if d > 0 else "sweep_high")
    sweeps_same = _count("sweep_high" if d > 0 else "sweep_low")
    c = df["c"]
    sma50 = float(c.iloc[max(0, t - 49):t + 1].mean())
    box = ev.get("box")
    ts_close = ctx.closes[t]
    f = {
        "is_box": int(box is not None), "dir": d, "impulse": int(bool(ctx.imp[t])),
        "body_atr": abs(entry - o) / atr, "body_dir_atr": (entry - o) / atr * d,
        "box_width_atr": (box["high"] - box["low"]) / atr if box else np.nan,
        "box_n": box["n_inside"] if box else np.nan,
        "risk_atr": risk / atr, "beyond_level_atr": (entry - ev["level"]) * d / atr,
        "htf_align": ctx.htf_trend[t] * d, "ltf_align": ctx.trend_num[t - 1] * d if t > 0 else 0,
        "sweep_opp_5": sweeps_opp, "sweep_same_5": sweeps_same,
        "dist_sma50_atr": (entry - sma50) / atr * d,
        "hour": ts_close.hour, "dow": ts_close.dayofweek, **_session_flags(ts_close),
    }
    return {"features": f, "entry": entry, "stop": stop, "risk": risk, "protected_idx": prot.idx}


def label_event(ctx, t: int, d: int, entry: float, stop: float, risk: float, k: int = STRUCT_K) -> Optional[int]:
    h, l = ctx.df["h"].to_numpy(float), ctx.df["l"].to_numpy(float)
    tp = entry + d * risk
    n = len(h)
    for j in range(t + 1, min(n, t + k + 1)):
        hit_stop = l[j] <= stop if d > 0 else h[j] >= stop
        hit_tp = h[j] >= tp if d > 0 else l[j] <= tp
        if hit_stop:
            return 0
        if hit_tp:
            return 1
    return 0 if t + k < n else None  # unresolved & not enough future -> unlabeled


def _train_structure(sym: Symbol, interval: str, k: int):
    from .structure import build_context
    ctx = build_context(sym, interval)
    rows, ys = [], []
    for ev in structure_events(ctx):
        fe = event_features(ctx, ev)
        if fe is None:
            continue
        y = label_event(ctx, ev["t"], ev["dir"], fe["entry"], fe["stop"], fe["risk"], k)
        if y is None:
            continue
        rows.append({**fe["features"], "_type": ev["type"]})
        ys.append(y)
    n = len(rows)
    base = {"symbol": sym.id, "interval": interval, "k_candles": k, "n_events": n, "engine": ENGINE,
            "label": f"+1R before protected-swing stop within {k} candles (same-candle touch = loss)",
            "source": ctx.source, "as_of": iso(ctx.df.index[-1])}
    if n < STRUCT_MIN_EVENTS:
        return {**base, "status": "insufficient_data", "min_required": STRUCT_MIN_EVENTS}
    X = pd.DataFrame(rows)
    types = X.pop("_type")
    y = np.array(ys)
    if len(np.unique(y)) < 2:
        return {**base, "status": "insufficient_data", "reason": "single class"}
    val = validate(X, y, 3)  # purge 3 events between train and test (labels span up to K candles)
    oos = val.pop("_oos", None)
    cal = fit_calibrator(*oos) if oos else None
    clf = _clf().fit(X, y)
    imp = np.asarray(getattr(clf, "feature_importances_", np.zeros(X.shape[1])), float)
    imp = imp / imp.sum() if imp.sum() > 0 else imp
    order = np.argsort(imp)[::-1][:8]
    by_type = {t: {"n": int((types == t).sum()), "hit_rate": float(y[(types == t).values].mean())}
               for t in STRUCT_TYPES if (types == t).sum()}
    return {**base, "status": "ok", "base_hit_rate": float(y.mean()), "by_type": by_type,
            "validation": {**val, "n_samples": n},
            "top_features": [{"feature": X.columns[i], "importance": round(float(imp[i]), 4)} for i in order],
            "trained_at": iso(datetime.now(timezone.utc)), "_clf": clf, "_cal": cal, "_cols": list(X.columns),
            "caveat": "Historical base rates of the house structure rules on this symbol/interval; label ignores "
                      "spread/slippage. Not financial advice."}


def structure_model(sym: Symbol, interval: str = "1h", k: int = STRUCT_K) -> dict:
    return cache.get_or_set(("struct_model", sym.id, interval, k, ENGINE), TTL_MODEL,
                            lambda: _train_structure(sym, interval, k))


def public_structure_model(m: dict) -> dict:
    return {k: v for k, v in m.items() if not k.startswith("_")}


def continuation_probability(m: dict, ctx, ev: dict) -> dict:
    fe = event_features(ctx, ev)
    if fe is None:
        return {"continuation_probability": None, "reason": "no protected swing / invalid risk"}
    if m.get("status") != "ok":
        return {"continuation_probability": None, "reason": m.get("status"), "risk_def": fe["risk"]}
    x = pd.DataFrame([fe["features"]])[m["_cols"]]
    raw = float(m["_clf"].predict_proba(x)[:, 1][0])
    v = m["validation"]
    return {"continuation_probability": apply_calibrator(m["_cal"], raw), "continuation_probability_raw": raw,
            "base_hit_rate": m["base_hit_rate"],
            "model_edge": "beat baseline OOS" if (v.get("brier_skill_score") or 0) > 0 else "no OOS edge (use base rate)"}
