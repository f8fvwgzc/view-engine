"""`setup:<interval>` heads: from this candle's close, which action reaches its target before its stop?

Three classes (buy / sell / hold) trained on rows exported by the quant sidecar, pooled over symbols and pip
settings. `long_tp1:<interval>` and `short_tp1:<interval>` are P(buy) and P(sell) of the same model.

Base estimator: LightGBM, chosen over sklearn's HistGradientBoostingClassifier for one reason: it returns
exact per-feature contributions (`pred_contrib`, TreeSHAP) at no extra cost, which is what `reasons` in
/v1/decide is built from. Both handle missing values natively and are comparably accurate on tabular data.
No class weights and no resampling: class balance differs wildly between instruments, and re-balancing would
make the calibrated probabilities stop being frequencies.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

import numpy as np
from lightgbm import LGBMClassifier
from sklearn.calibration import CalibratedClassifierCV
from sklearn.model_selection import TimeSeriesSplit

from ..contract import ContractError

CLASSES = ("buy", "sell", "hold")
LABEL = {c: i for i, c in enumerate(CLASSES)}
INTERVAL_SECONDS = {"5m": 300, "15m": 900, "1h": 3600}
# sklearn warns that isotonic calibration overfits with few samples (it suggests >> 1000). It is used only
# when every class has at least this many rows in every calibration fold; otherwise sigmoid (Platt).
ISOTONIC_MIN_PER_CLASS = 3000


class NotEnoughData(Exception):
    pass


def booster(n_rows: int, n_estimators: int = 300, seed: int = 7) -> LGBMClassifier:
    """Small, heavily regularised trees: the signal in price structure is weak and the rows are correlated.
    The objective follows the labels (three outcomes for a setup head, yes/no for a pattern head)."""
    return LGBMClassifier(
        n_estimators=n_estimators, learning_rate=0.03, num_leaves=15, max_depth=5,
        min_child_samples=int(max(20, min(300, n_rows // 150))), subsample=0.8, subsample_freq=1,
        colsample_bytree=0.7, reg_lambda=5.0, random_state=seed, deterministic=True, force_row_wise=True,
        verbose=-1)


def time_ordered_folds(t: np.ndarray, t_end: np.ndarray, n_splits: int = 5, gap: int = 48) -> list[tuple]:
    """Expanding-window folds for calibration: fit on the past, calibrate on the block that follows.

    Fold boundaries come from sklearn's TimeSeriesSplit over the distinct timestamps (so one candle time is
    never split between the two sides) with `gap` timestamps left out before each calibration block. On top
    of that a training row is kept only if its label window had closed (`t_end`) by the first calibration
    timestamp. With pooled symbols a gap counted in rows or timestamps is not a gap in each symbol's own
    candles (sessions differ, weekends); the label-end purge is exact per symbol.
    """
    t, t_end = np.asarray(t), np.asarray(t_end)
    uniq = np.unique(t)
    n_splits = int(min(n_splits, max(1, (len(uniq) - gap) // 50 - 1)))
    if n_splits < 2 or len(uniq) <= n_splits + 1 + gap:
        raise NotEnoughData(f"only {len(uniq)} distinct candle times: not enough for time-ordered calibration "
                            f"folds with a {gap}-candle gap")
    folds = []
    for tr_u, te_u in TimeSeriesSplit(n_splits=n_splits, gap=gap).split(uniq):
        cal_start, cal_end, tr_end = uniq[te_u[0]], uniq[te_u[-1]], uniq[tr_u[-1]]
        train = np.flatnonzero((t <= tr_end) & (t_end <= cal_start))
        cal = np.flatnonzero((t >= cal_start) & (t <= cal_end))
        folds.append((train, cal))
    return folds


def usable_folds(folds: Sequence[tuple], y: np.ndarray, n_classes: int, min_train: int = 60) -> list[tuple]:
    """Folds whose training side has every class (the booster needs them all) and enough rows."""
    return [(tr, ca) for tr, ca in folds
            if len(tr) >= min_train and len(ca) > 0 and len(np.unique(y[tr])) == n_classes]


def calibration_method(folds: Sequence[tuple], y: np.ndarray, n_classes: int) -> str:
    least = min(int(np.bincount(y[ca], minlength=n_classes).min()) for _, ca in folds)
    return "isotonic" if least >= ISOTONIC_MIN_PER_CLASS else "sigmoid"


def fit(X, y, w, t, t_end, *, horizon: int, n_estimators: int = 300, seed: int = 7, n_splits: int = 5,
        n_classes: int = 3):
    """CalibratedClassifierCV(ensemble=True) over time-ordered folds: one booster per fold, each calibrated on
    the block after its training window; predictions average the calibrated members."""
    folds = usable_folds(time_ordered_folds(t, t_end, n_splits, horizon), y, n_classes)
    if not folds:
        raise NotEnoughData("no calibration fold has every outcome in its training window")
    method = calibration_method(folds, y, n_classes)
    model = CalibratedClassifierCV(booster(len(y), n_estimators, seed), method=method, cv=folds, ensemble=True)
    model.fit(X, y, sample_weight=w)
    return model, {"method": method, "folds": [{"n_train": int(len(tr)), "n_calibration": int(len(ca))}
                                               for tr, ca in folds]}


# ----------------------------------------------------------------------------- scoring

def check_vector(artifact: dict, feature_version: Any, feature_names: Any, x: Any) -> np.ndarray:
    """Refuse a feature vector the model was not trained on (HTTP 422)."""
    head = artifact["head"]
    if feature_version != artifact["feature_version"]:
        raise ContractError(
            f"feature_version mismatch: head '{head}' ({artifact.get('model_version')}) was trained on "
            f"'{artifact['feature_version']}', got {feature_version!r}. Retrain with POST /v1/train or send "
            f"features of the trained version.", kind="feature_version_mismatch",
            expected=artifact["feature_version"], got=feature_version)
    if feature_names is not None and list(feature_names) != list(artifact["feature_names"]):
        raise ContractError(f"feature_names do not match the {len(artifact['feature_names'])} names head "
                            f"'{head}' was trained on (same feature_version, different columns)",
                            kind="feature_names_mismatch")
    if not isinstance(x, (list, tuple)) or len(x) != len(artifact["feature_names"]):
        raise ContractError(f"x must be an array of {len(artifact['feature_names'])} numbers (null = missing) "
                            f"in feature_names order", kind="bad_feature_vector")
    try:  # float32 like the training matrix; null -> NaN (the booster handles missing values natively)
        return np.array([[np.nan if v is None else float(v) for v in x]], np.float32)
    except (TypeError, ValueError):
        raise ContractError("x must contain only numbers or null", kind="bad_feature_vector")


def predict(artifact: dict, x: np.ndarray) -> np.ndarray:
    """Calibrated [P(buy), P(sell), P(hold)] for one feature row of shape (1, n_features)."""
    return artifact["model"].predict_proba(x)[0]


def contributions(artifact: dict, x: np.ndarray, class_index: int) -> Optional[np.ndarray]:
    """Per-feature contribution to the score of one class for this row: LightGBM TreeSHAP values (log-odds
    of the uncalibrated booster), averaged over the calibrated ensemble's members. For a yes/no model
    `class_index` 1 is "yes". None when the members cannot explain themselves."""
    f = len(artifact["feature_names"])
    total, used = np.zeros(f), 0
    try:
        for member in artifact["model"].calibrated_classifiers_:
            est = member.estimator
            k = len(getattr(est, "classes_", []))
            c = np.asarray(est.predict(x, pred_contrib=True))
            if k == 2 and c.size == f + 1:          # binary: one block, for the positive class
                row = c.reshape(f + 1)[:f] * (1.0 if class_index == 1 else -1.0)
            elif c.size == k * (f + 1):
                row = c.reshape(k, f + 1)[class_index, :f]
            else:
                continue
            total += row
            used += 1
    except Exception:
        return None
    return total / used if used else None


def top_features(artifact: dict, x: np.ndarray, contrib: np.ndarray, top: int = 6) -> list[dict]:
    names = artifact["feature_names"]
    order = np.argsort(-np.abs(contrib))[:top]
    return [{"feature": names[i], "value": None if np.isnan(x[0, i]) else round(float(x[0, i]), 4),
             "contribution": round(float(contrib[i]), 4)} for i in order if abs(contrib[i]) > 1e-9]


GROUPS = ("session", "news", "structure", "zones", "strength", "volatility", "candle")
_GROUP_RULES = (   # fallback for feature sets that ship no `feature_groups` (structure-v1)
    ("news", ("news", "event", "calendar")),
    ("session", ("hour_", "dow", "sess_", "mins_since_session", "session")),
    ("zones", ("sup_", "res_", "zone")),
    ("volatility", ("atr_pips", "atr_vs", "sl_atr", "tp_atr", "tp2_atr", "range_atr", "volatility")),
    ("candle", ("body_", "wick", "close_pos", "form_")),
    ("strength", ("ret", "align_", "imp_", "regime_impulse")),
    ("structure", ("regime", "swing", "pullback", "box", "sweeps", "fakeouts", "brk_", "last_")),
)


def feature_group(name: str, groups: Optional[dict]) -> str:
    if groups and name in groups:
        return str(groups[name])
    core = name.split("_", 1)[1] if name.split("_", 1)[0] in ("tr", "ht1", "ht2") and "_" in name else name
    for group, needles in _GROUP_RULES:
        if any(core.startswith(n) or n in core for n in needles):
            return group
    return "other"


def group_shares(artifact: dict, contrib: np.ndarray) -> dict[str, float]:
    """Share of the absolute attribution carried by each feature group; the shares sum to 1."""
    total = float(np.abs(contrib).sum())
    if total <= 0:
        return {}
    out: dict[str, float] = {}
    for name, c in zip(artifact["feature_names"], contrib):
        g = feature_group(name, artifact.get("feature_groups"))
        out[g] = out.get(g, 0.0) + abs(float(c)) / total
    return {g: round(v, 4) for g, v in sorted(out.items(), key=lambda kv: -kv[1])}


SESSION_COLUMNS = (("overlap", "sess_overlap"), ("london", "sess_london"), ("newyork", "sess_newyork"),
                   ("asia", "sess_asia"))


def sessions(X: np.ndarray, names: Sequence[str], t: np.ndarray) -> list[str]:
    """Trading session of each row: from the sidecar's session flags when the feature set has them, else from
    the UTC hour of the candle close (asia 0-7, london 7-12, overlap 12-16, newyork 16-21)."""
    names = list(names)
    if all(col in names for _, col in SESSION_COLUMNS):
        out = np.full(len(X), "off", dtype=object)
        for label, col in reversed(SESSION_COLUMNS):
            out[np.nan_to_num(X[:, names.index(col)]) > 0.5] = label
        return out.tolist()
    hour = (np.asarray(t, np.int64) // 3600) % 24
    return np.select([hour < 7, hour < 12, hour < 16, hour < 21], ["asia", "london", "overlap", "newyork"],
                     "off").tolist()


def applicable(artifact: dict, x: np.ndarray, declared: Any = None) -> bool:
    """Is the chart in the situation a pattern head was trained on? The sidecar's own statement wins
    (`applicable[label]` in /features or in the state); otherwise a gate model trained to reproduce where the
    label existed in the training rows decides; with neither, the head always applies."""
    if isinstance(declared, dict) and artifact.get("label") in declared:
        return bool(declared[artifact["label"]])
    gate = artifact.get("gate")
    if gate is None:
        return True
    return bool(gate.predict_proba(x)[0, 1] >= 0.5)
