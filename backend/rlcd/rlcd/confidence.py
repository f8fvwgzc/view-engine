"""Confidence formulas of the reference (System One docs, "confidence").

All three map a probability distribution to [0, 1]: 0 = indistinguishable from chance, 1 = certain.
Confidence is a statement about the shape of the distribution, not about whether it is right; whether the
probabilities can be trusted is what the calibration report (`/v1/calibration`) is for.
"""
from __future__ import annotations

from typing import Sequence


def noul_confidence(p: float) -> float:
    """|2p - 1|: distance of a yes-probability from a coin flip."""
    return abs(2.0 * float(p) - 1.0)


def choice_confidence(probabilities: Sequence[float]) -> float:
    """(p_max - 1/n) / (1 - 1/n): how far the top option sits above the uniform distribution."""
    n = len(probabilities)
    if n < 2:
        return 1.0 if n == 1 else 0.0
    p_max = max(float(p) for p in probabilities)
    return max(0.0, min(1.0, (p_max - 1.0 / n) / (1.0 - 1.0 / n)))


def score_confidence(probabilities: Sequence[float]) -> float:
    """max(0, 1 - sum_i p_i*|i - m| / MAD_unif), m = most likely level (first one on ties),
    MAD_unif = (1/n) * sum_i |i - (n-1)/2|. Unlike the choice formula it is distance aware:
    mass on an adjacent level costs less than mass on the opposite end."""
    n = len(probabilities)
    if n < 2:
        return 1.0 if n == 1 else 0.0
    ps = [float(p) for p in probabilities]
    m = max(range(n), key=lambda i: (ps[i], -i))
    spread = sum(p * abs(i - m) for i, p in enumerate(ps))
    mad_unif = sum(abs(i - (n - 1) / 2.0) for i in range(n)) / n
    return max(0.0, 1.0 - spread / mad_unif)
