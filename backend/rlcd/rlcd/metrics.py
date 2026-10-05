"""Calibration metrics. Everything here is computed on rows the model never saw (the holdout).

Definitions used throughout the service:
  * Brier (multiclass)  mean over rows of sum_k (p_k - y_k)^2   (0 = perfect, 2 = worst)
  * Brier (binary)      mean (p - y)^2
  * ECE                 sum over 10 equal-width bins of (count/n) * |mean predicted - observed rate|
  * baseline            the base-rate forecaster: always predicts the class frequencies of the training rows
  * Brier skill score   1 - brier / brier_baseline   (> 0 = better than the base rate)
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

import numpy as np

EPS = 1e-12
BINS = 10


def _f(x: float) -> Optional[float]:
    x = float(x)
    return round(x, 6) if math.isfinite(x) else None


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[Optional[float], Optional[float]]:
    """Wilson score interval for a binomial rate (95% by default)."""
    if n <= 0:
        return None, None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def reliability_table(p: np.ndarray, y: np.ndarray, bins: int = BINS) -> list[dict]:
    """10 equal-width probability bins: mean predicted probability, observed rate, count."""
    p, y = np.asarray(p, float), np.asarray(y, float)
    idx = np.minimum((p * bins).astype(int), bins - 1)
    out = []
    for b in range(bins):
        m = idx == b
        n = int(m.sum())
        out.append({"bin": b, "lo": round(b / bins, 2), "hi": round((b + 1) / bins, 2), "count": n,
                    "mean_predicted": _f(p[m].mean()) if n else None,
                    "observed_rate": _f(y[m].mean()) if n else None})
    return out


def ece(p: np.ndarray, y: np.ndarray, bins: int = BINS) -> float:
    p, y = np.asarray(p, float), np.asarray(y, float)
    if len(p) == 0:
        return float("nan")
    idx = np.minimum((p * bins).astype(int), bins - 1)
    total = 0.0
    for b in range(bins):
        m = idx == b
        if m.any():
            total += m.mean() * abs(p[m].mean() - y[m].mean())
    return float(total)


def brier_rows(P: np.ndarray, Y: np.ndarray) -> np.ndarray:
    return ((P - Y) ** 2).sum(axis=1)


def log_loss_rows(P: np.ndarray, Y: np.ndarray) -> np.ndarray:
    return -(Y * np.log(np.clip(P, EPS, 1.0))).sum(axis=1)


def binary_report(p: np.ndarray, y: np.ndarray, base_rate) -> dict:
    """Reliability of one yes-probability against its 0/1 outcome, next to the base-rate forecaster
    (`base_rate`: a scalar, or one baseline probability per row)."""
    p, y = np.asarray(p, float), np.asarray(y, float)
    b = np.broadcast_to(np.asarray(base_rate, float), p.shape).astype(float)
    brier, brier_b = float(((p - y) ** 2).mean()), float(((b - y) ** 2).mean())

    def ll(q):
        q = np.clip(q, EPS, 1 - EPS)
        return float(-(y * np.log(q) + (1 - y) * np.log(1 - q)).mean())

    return {"n": int(len(p)), "observed_rate": _f(y.mean()), "base_rate_train": _f(b.mean()),
            "brier": _f(brier), "log_loss": _f(ll(p)), "ece": _f(ece(p, y)),
            "baseline": {"brier": _f(brier_b), "log_loss": _f(ll(b)), "ece": _f(ece(b, y))},
            "brier_skill_score": _f(1 - brier / brier_b) if brier_b > 0 else None,
            "reliability": reliability_table(p, y)}


def skill_test(gain: np.ndarray, blocks: int = 20) -> dict:
    """Is the per-row Brier gain over the base rate reliably positive?

    Rows in a time-ordered holdout are autocorrelated (labels overlap for `horizon` candles), so a per-row
    standard error would be far too small. The gain is averaged inside `blocks` contiguous blocks and a
    one-sided t interval is taken across block means: skill is claimed only when its 95% lower bound is > 0.
    """
    gain = np.asarray(gain, float)
    n = len(gain)
    blocks = max(2, min(blocks, n // 5)) if n >= 10 else 0
    if not blocks:
        return {"has_skill": False, "mean_brier_gain": _f(gain.mean()) if n else None, "lower_95": None,
                "blocks": 0, "method": "too few holdout rows to test"}
    means = np.array([c.mean() for c in np.array_split(gain, blocks)])
    m, sd = float(means.mean()), float(means.std(ddof=1))
    from scipy import stats
    lower = m - float(stats.t.ppf(0.95, blocks - 1)) * sd / math.sqrt(blocks)
    return {"has_skill": bool(m > 0 and lower > 0), "mean_brier_gain": _f(m), "lower_95": _f(lower),
            "blocks": int(blocks),
            "method": "one-sided 95% t bound on the Brier gain over the base rate, across contiguous blocks"}


def calibration_report(P: np.ndarray, y: np.ndarray, classes: Sequence[str], prior: np.ndarray,
                       baseline_note: str = "always predicts the class frequencies of the training rows") -> dict:
    """Full holdout report of a calibrated K-class forecaster. `y` holds class indices. `prior` is the
    baseline forecast: one row of training class frequencies, or one such row per holdout row (the setup
    heads use the frequencies of each row's own instrument, which is the harder baseline to beat)."""
    P, y, prior = np.asarray(P, float), np.asarray(y, int), np.asarray(prior, float)
    n, k = P.shape
    Y = np.eye(k)[y]
    B = np.tile(prior, (n, 1)) if prior.ndim == 1 else prior
    brier, brier_b = brier_rows(P, Y), brier_rows(B, Y)
    hit = (P.argmax(axis=1) == y).astype(float)
    hit_b = (B.argmax(axis=1) == y).astype(float)
    conf, conf_b = P.max(axis=1), B.max(axis=1)
    per_class = {c: binary_report(P[:, i], Y[:, i], B[:, i]) for i, c in enumerate(classes)}
    bm, bbm = float(brier.mean()), float(brier_b.mean())
    return {
        "n_holdout": int(n), "classes": list(classes),
        "class_rates_holdout": {c: _f(Y[:, i].mean()) for i, c in enumerate(classes)},
        "base_rates_train": {c: _f(B[:, i].mean()) for i, c in enumerate(classes)},
        "brier": _f(bm), "log_loss": _f(log_loss_rows(P, Y).mean()), "accuracy": _f(hit.mean()),
        "ece": _f(ece(conf, hit)),
        "ece_classwise": _f(np.mean([per_class[c]["ece"] for c in classes])),
        "baseline": {"brier": _f(bbm), "log_loss": _f(log_loss_rows(B, Y).mean()), "accuracy": _f(hit_b.mean()),
                     "ece": _f(ece(conf_b, hit_b)),
                     "ece_classwise": _f(np.mean([per_class[c]["baseline"]["ece"] for c in classes]))},
        "brier_skill_score": _f(1 - bm / bbm) if bbm > 0 else None,
        "skill": skill_test(brier_b - brier),
        "reliability": {"top_label": reliability_table(conf, hit),
                        "per_class": {c: per_class[c]["reliability"] for c in classes}},
        "per_class": {c: {k_: v for k_, v in per_class[c].items() if k_ != "reliability"} for c in classes},
        "definitions": {
            "brier": "mean over rows of sum_k (p_k - y_k)^2",
            "ece": "top-label expected calibration error over 10 equal-width bins",
            "baseline": baseline_note,
            "brier_skill_score": "1 - brier / baseline brier; > 0 means better than the base rate",
        },
    }


def summary(report: dict) -> dict:
    """The few numbers shown in /v1/models, /v1/heads and /v1/decide."""
    keys = ("n_holdout", "brier", "log_loss", "accuracy", "ece", "brier_skill_score")
    out = {k: report.get(k) for k in keys}
    out["baseline_brier"] = report.get("baseline", {}).get("brier")
    out["baseline_log_loss"] = report.get("baseline", {}).get("log_loss")
    out["baseline_accuracy"] = report.get("baseline", {}).get("accuracy")
    out["has_skill"] = bool(report.get("skill", {}).get("has_skill"))
    return out
