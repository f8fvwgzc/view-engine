"""Every decision as a Bernoulli trial.

A trade with a fixed stop and target either reaches the target first (success, probability p) or it does not.
With payoff b = tp / sl (in units of the risk, R) and a cost in R:

    expected R      E = p*b - (1 - p) - cost
    variance of R   V = p*(1 - p)*(b + 1)^2
    breakeven       p* = (1 + cost) / (b + 1)
    Kelly fraction  f* = p - (1 - p)/b            (0 when negative: no edge, no stake)
    log growth      g(f) = p*ln(1 + f*b) + (1 - p)*ln(1 - f)   per trade, staking fraction f of the account

The calibrated probability says what the model believes; the Beta-Bernoulli posterior says what has actually
happened to predictions like this one. Counts are kept per (head, action, probability bin) and per
(symbol, session), start from the non-overlapping outcomes of the holdout, and move with every scored outcome
posted to /v1/feedback with a decision_id. No retrain is involved: that online update is the reinforcement
step. `act` requires the posterior to put at least `min_p_above_breakeven` of its mass above breakeven.
"""
from __future__ import annotations

import json
import math
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from scipy import stats

BINS = 10
SIDES = ("buy", "sell")


@dataclass(frozen=True)
class BernoulliConfig:
    prior_alpha: float = 1.0             # Beta(1, 1): uniform prior on the true win rate
    prior_beta: float = 1.0
    min_p_above_breakeven: float = 0.9   # the gate for `act`
    kelly_multiplier: float = 0.25       # the reported stake is quarter Kelly ...
    kelly_cap: float = 0.02              # ... and never more than 2% of the account at risk per trade
    alpha: float = 0.05                  # one-sided significance for the binomial test and the sample size
    power: float = 0.8

    def to_dict(self) -> dict:
        return asdict(self)


def _r(x: Optional[float], nd: int = 6) -> Optional[float]:
    return None if x is None or not math.isfinite(x) else round(float(x), nd)


# ----------------------------------------------------------------------------- one trial

def expected_r(p: float, b: float, cost: float = 0.0) -> float:
    return p * b - (1.0 - p) - cost


def variance_r(p: float, b: float) -> float:
    return p * (1.0 - p) * (b + 1.0) ** 2


def breakeven(b: float, cost: float = 0.0) -> float:
    return (1.0 + cost) / (b + 1.0)


def kelly(p: float, b: float) -> float:
    """f* = p - (1 - p)/b, floored at 0. p = 0.4, b = 2.5 -> 0.16."""
    if b <= 0:
        return 0.0
    return max(0.0, p - (1.0 - p) / b)


def log_growth(p: float, b: float, f: float) -> float:
    """Expected log growth of the account per trade when a fraction f of it is risked."""
    if f <= 0:
        return 0.0
    if f >= 1:
        return float("-inf")
    return p * math.log1p(f * b) + (1.0 - p) * math.log1p(-f)


def sizing(p: float, b: float, cfg: BernoulliConfig = BernoulliConfig()) -> dict:
    full = kelly(p, b)
    stake = min(cfg.kelly_cap, full * cfg.kelly_multiplier)
    return {"kelly": _r(full), "kelly_quarter": _r(stake), "kelly_multiplier": cfg.kelly_multiplier,
            "kelly_cap": cfg.kelly_cap, "capped": bool(full * cfg.kelly_multiplier > cfg.kelly_cap),
            "log_growth_per_trade": _r(log_growth(p, b, stake), 8)}


# ----------------------------------------------------------------------------- evidence about the win rate

def posterior(wins: float, losses: float, p0: float, cfg: BernoulliConfig = BernoulliConfig()) -> dict:
    """Beta(prior_alpha + wins, prior_beta + losses) for the true win rate, against breakeven p0."""
    a, b = cfg.prior_alpha + wins, cfg.prior_beta + losses
    lo, hi = stats.beta.ppf([0.025, 0.975], a, b)
    return {"alpha": _r(a, 3), "beta": _r(b, 3), "n": int(round(wins + losses)), "mean": _r(a / (a + b)),
            "ci95": [_r(lo), _r(hi)], "breakeven": _r(p0),
            "p_above_breakeven": _r(float(stats.beta.sf(p0, a, b)))}


def thompson(wins: float, losses: float, rng: np.random.Generator,
             cfg: BernoulliConfig = BernoulliConfig()) -> float:
    """One draw of the win rate from its posterior (exploration: under-sampled bins get tried sometimes)."""
    return float(rng.beta(cfg.prior_alpha + wins, cfg.prior_beta + losses))


def binomial_test(wins: int, n: int, p0: float) -> Optional[float]:
    """Exact one-sided p-value: P(X >= wins) for X ~ Binomial(n, p0). Small = the win rate beats p0."""
    if n <= 0:
        return None
    return float(stats.binom.sf(wins - 1, n, p0))


def trades_needed(p1: Optional[float], p0: float, cfg: BernoulliConfig = BernoulliConfig()) -> Optional[int]:
    """Trades needed to tell a true win rate p1 from breakeven p0 (one-sided test at `alpha`, `power`):
    n = ((z_a*sqrt(p0*(1-p0)) + z_b*sqrt(p1*(1-p1))) / (p1 - p0))^2. None when p1 is not above p0."""
    if p1 is None or not p0 < p1 < 1:
        return None
    za, zb = stats.norm.ppf(1 - cfg.alpha), stats.norm.ppf(cfg.power)
    n = ((za * math.sqrt(p0 * (1 - p0)) + zb * math.sqrt(p1 * (1 - p1))) / (p1 - p0)) ** 2
    return int(math.ceil(n))


def evidence(stats_: dict, cfg: BernoulliConfig = BernoulliConfig()) -> dict:
    """Binomial test and sample size for a set of holdout trades (`policy.trade_stats` output)."""
    n, wins, p0 = stats_["n"], stats_["wins"], stats_["breakeven_rate"]
    if not n:
        return {"n": 0, "p_value": None, "trades_needed_80_power": None}
    return {"n": n, "wins": wins, "win_rate": stats_["win_rate"], "breakeven": p0,
            "p_value": _r(binomial_test(wins, n, p0), 8),
            "test": "exact one-sided binomial test of the win rate against breakeven",
            "trades_needed_80_power": trades_needed(stats_["win_rate"], p0, cfg)}


def bin_of(p: float) -> int:
    return int(min(BINS - 1, max(0, math.floor(float(p) * BINS))))


# ----------------------------------------------------------------------------- the durable table

class Ledger:
    """Win/loss counts per head: `bins[side][k]` for predictions whose P(side) fell in bin k, and
    `symbol_session["XAUUSD|london"]` for signals taken. Stored as bernoulli.json under RLCD_HOME."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    def _read(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except json.JSONDecodeError:
                return {}
        return {}

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self.path)

    @staticmethod
    def empty(model_version: Optional[str] = None) -> dict:
        return {"model_version": model_version, "bins": {s: [[0, 0] for _ in range(BINS)] for s in SIDES},
                "symbol_session": {}, "holdout_trials": 0, "online_trials": 0, "applied": []}

    def table(self, head: str) -> dict:
        with self._lock:
            return self._read().get(head) or self.empty()

    def reset(self, head: str, table: dict) -> None:
        """Start a head's table again from the holdout of a freshly trained model."""
        with self._lock:
            data = self._read()
            data[head] = table
            self._write(data)

    def update(self, head: str, decision: dict, label: str) -> Optional[dict]:
        """Score one decision against its realised outcome. Each decision_id counts once."""
        with self._lock:
            data = self._read()
            t = data.get(head) or self.empty()
            did = decision["decision_id"]
            if did in t["applied"]:
                return None
            probs, changed = decision.get("probabilities") or {}, {}
            for side in SIDES:   # both sides were predicted, so both are trials of their own bin
                if side in probs:
                    k = bin_of(probs[side])
                    t["bins"][side][k][0 if label == side else 1] += 1
                    changed[f"{side}:bin{k}"] = t["bins"][side][k]
            if decision.get("action") in SIDES:   # a signal was given: a trial of that symbol and session
                key = f"{decision.get('symbol')}|{decision.get('session') or 'unknown'}"
                cell = t["symbol_session"].setdefault(key, [0, 0])
                cell[0 if label == decision["action"] else 1] += 1
                changed[key] = cell
            t["applied"].append(did)
            t["online_trials"] = t.get("online_trials", 0) + 1
            data[head] = t
            self._write(data)
            return changed


def holdout_table(P: np.ndarray, y: np.ndarray, taken: np.ndarray, side: np.ndarray, symbols: list[str],
                  sessions: list[str], independent: np.ndarray, model_version: Optional[str] = None) -> dict:
    """Initial counts from the holdout. `P` is (n, 3) [buy, sell, hold], `y` the realised class index.
    Probability bins use the rows in `independent` only (one per label horizon per instrument, so the trials
    do not share candles); symbol/session cells use the non-overlapping signals in `taken`."""
    t = Ledger.empty(model_version)
    idx = np.flatnonzero(independent)
    for s, side_name in enumerate(SIDES):
        k = np.minimum((P[idx, s] * BINS).astype(int), BINS - 1)
        win = y[idx] == s
        for b in range(BINS):
            m = k == b
            t["bins"][side_name][b] = [int((m & win).sum()), int((m & ~win).sum())]
    for i in np.flatnonzero(taken):
        cell = t["symbol_session"].setdefault(f"{symbols[i]}|{sessions[i]}", [0, 0])
        cell[0 if y[i] == side[i] else 1] += 1
    t["holdout_trials"] = int(len(idx))
    return t


def table_view(table: dict, p0: float, cfg: BernoulliConfig = BernoulliConfig()) -> dict:
    """The posterior table for /v1/bernoulli: one row per (action, probability bin) and per symbol|session."""
    bins = []
    for side in SIDES:
        for k, (w, l) in enumerate(table["bins"][side]):
            bins.append({"action": side, "bin": k, "lo": round(k / BINS, 2), "hi": round((k + 1) / BINS, 2),
                         "wins": w, "losses": l, **posterior(w, l, p0, cfg)})
    cells = {key: {"wins": w, "losses": l, **posterior(w, l, p0, cfg)}
             for key, (w, l) in sorted(table["symbol_session"].items())}
    return {"bins": bins, "symbol_session": cells, "holdout_trials": table.get("holdout_trials", 0),
            "online_trials": table.get("online_trials", 0), "model_version": table.get("model_version")}
