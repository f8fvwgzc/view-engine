"""`intent` and `trade_style` heads: what is the user asking for?

Model: TF-IDF (word 1-2 grams + char 3-5 grams) plus a few meta features (attachments, timeframes, symbol)
-> LogisticRegression, wrapped in CalibratedClassifierCV(method="sigmoid", cv=5). No class re-weighting, so
the calibrated probabilities are frequencies under the training mix.
"""
from __future__ import annotations

import re
from typing import Any, Optional, Sequence

import numpy as np
from scipy import sparse
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.calibration import CalibratedClassifierCV
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression

from ..contract import ContractError

_TF_RE = re.compile(r"^\s*(?:(m|h|d|w)\s*(\d{1,3})|(\d{1,3})\s*(m|min|minute|minutes|h|hr|hour|hours|d|day|w|week))\s*$",
                    re.I)
_UNIT = {"m": 1, "min": 1, "minute": 1, "minutes": 1, "h": 60, "hr": 60, "hour": 60, "hours": 60, "d": 1440,
         "day": 1440, "w": 10080, "week": 10080}
_WORD_TF = {"daily": 1440, "weekly": 10080, "monthly": 43200, "hourly": 60}
META = ["has_attachments", "attachments", "n_timeframes", "tf_le_1h", "tf_4h", "tf_ge_1d", "has_symbol",
        "n_words", "is_short", "is_empty"]


def tf_minutes(tf: Any) -> Optional[int]:
    """'M15', '15m', 'h1', '1 hour', 'D1', 'weekly' -> minutes; None if it is not a timeframe."""
    s = str(tf).strip().lower()
    if s in _WORD_TF:
        return _WORD_TF[s]
    m = _TF_RE.match(s)
    if not m:
        return None
    unit, num = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
    return int(num) * _UNIT[unit.lower()]


def normalize_state(state: Any) -> dict:
    """Request state -> {text, attachments, timeframes, symbol}. Accepts a bare string or any subset."""
    if state is None:
        state = {}
    if isinstance(state, str):
        state = {"text": state}
    if not isinstance(state, dict):
        raise ContractError("intent heads need `state` to be a string or an object with any of: text, "
                            "attachments, timeframes, symbol")
    text = state.get("text")
    if text is not None and not isinstance(text, str):
        raise ContractError("state.text must be a string")
    att = state.get("attachments") or 0
    if isinstance(att, (list, tuple)):
        att = len(att)
    if isinstance(att, bool) or not isinstance(att, (int, float)) or att < 0:
        raise ContractError("state.attachments must be a non-negative integer (or a list, which is counted)")
    tfs = state.get("timeframes") or []
    if isinstance(tfs, str):
        tfs = [t for t in re.split(r"[,\s]+", tfs) if t]
    if not isinstance(tfs, (list, tuple)):
        raise ContractError("state.timeframes must be an array of strings")
    sym = state.get("symbol")
    return {"text": (text or "")[:4000], "attachments": int(att), "timeframes": [str(t) for t in tfs][:12],
            "symbol": str(sym) if sym else None}


def meta_features(s: dict) -> list[float]:
    mins = [m for m in (tf_minutes(t) for t in s["timeframes"]) if m]
    words = len(s["text"].split())
    return [float(s["attachments"] > 0), min(s["attachments"], 5) / 5.0, min(len(s["timeframes"]), 4) / 4.0,
            float(any(m <= 60 for m in mins)), float(any(60 < m < 1440 for m in mins)),
            float(any(m >= 1440 for m in mins)), float(bool(s["symbol"])), min(words, 40) / 40.0,
            float(words <= 3), float(words == 0)]


class IntentClassifier(ClassifierMixin, BaseEstimator):
    """Featurizer + logistic regression as one estimator, so sample weights (feedback rows are up-weighted)
    reach the regression when it is fitted inside CalibratedClassifierCV. X is a sequence of state dicts."""

    def __init__(self, C: float = 8.0, max_features: int = 30000):
        self.C = C
        self.max_features = max_features

    def _matrix(self, X: Sequence[dict], fit: bool = False):
        texts = [s["text"].lower() for s in X]
        meta = sparse.csr_matrix(np.array([meta_features(s) for s in X], float))
        if fit:
            self.word_ = TfidfVectorizer(analyzer="word", ngram_range=(1, 2), token_pattern=r"(?u)\b\w+\b",
                                         sublinear_tf=True, min_df=1, max_features=self.max_features)
            self.char_ = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True, min_df=2,
                                         max_features=self.max_features)
            w, c = self.word_.fit_transform(texts), self.char_.fit_transform(texts)
        else:
            w, c = self.word_.transform(texts), self.char_.transform(texts)
        return sparse.hstack([w, c, meta], format="csr")

    def fit(self, X, y, sample_weight=None):
        X = list(X)
        self.clf_ = LogisticRegression(C=self.C, max_iter=2000)
        self.clf_.fit(self._matrix(X, fit=True), np.asarray(y), sample_weight=sample_weight)
        self.classes_ = self.clf_.classes_
        return self

    def decision_function(self, X):
        return self.clf_.decision_function(self._matrix(list(X)))

    def predict_proba(self, X):
        return self.clf_.predict_proba(self._matrix(list(X)))

    def predict(self, X):
        return self.clf_.predict(self._matrix(list(X)))


def as_array(states: Sequence[dict]) -> np.ndarray:
    """1-D object array, so sklearn's fold indexing treats each state as one sample."""
    arr = np.empty(len(states), dtype=object)
    for i, s in enumerate(states):
        arr[i] = s
    return arr


def fit(states: Sequence[dict], labels: Sequence[int], weights: Sequence[float], seed: int = 7):
    model = CalibratedClassifierCV(IntentClassifier(), method="sigmoid", cv=5)
    model.fit(as_array(states), np.asarray(labels, int), sample_weight=np.asarray(weights, float))
    return model


def predict(artifact: dict, state: Any) -> np.ndarray:
    """Calibrated probabilities in the order of the head's classes."""
    return artifact["model"].predict_proba(as_array([normalize_state(state)]))[0]
