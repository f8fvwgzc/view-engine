"""The decision policy: turn calibrated outcome probabilities into act / confirm / hold.

The bandit view: context = features, actions = buy / sell / hold, reward = realised R of the action
(hold = 0). With a fixed stop and target the expected reward of a trade is a function of one number, the
calibrated probability p that the target is reached before the stop:

    breakeven   p* = (sl + cost) / (sl + tp)
    expected R     = p * tp / sl - (1 - p) - cost / sl

(every non-win is counted as a full stop-out, which is conservative: a trade that times out inside the
horizon loses less than 1R). The policy is a gate on that number, not a learned controller:

    act      the best side is the single most likely outcome, p >= p* + act_margin, confidence >= act_confidence
    confirm  p >= p* + confirm_margin and confidence >= confirm_confidence   (wait for the trigger candle)
    hold     otherwise

and it is capped by what the untouched holdout showed (`verify`): no skill over the base rate -> always hold;
an edge that is not statistically established -> never more than confirm.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Optional

import numpy as np

from .metrics import _f, wilson

BUY, SELL, HOLD = 0, 1, 2
ACTIONS = ("buy", "sell", "hold")
TIERS = ("hold", "confirm", "act")  # index = strength


@dataclass(frozen=True)
class PolicyConfig:
    """All thresholds of the policy, in one place. `/v1/train` tunes act_margin and act_confidence on
    validation rows (never on the holdout); everything else stays at its default."""
    act_margin: float = 0.05          # probability points above breakeven needed to act
    act_confidence: float = 0.15      # choice confidence (p_max - 1/3) / (2/3) needed to act
    confirm_margin: float = 0.02      # probability points above breakeven needed to ask for confirmation
    confirm_confidence: float = 0.0
    default_cost_pips: float = 1.0    # spread assumed when neither the request nor the training data has one
    min_trades: int = 100             # tuning: fewest validation trades a threshold pair must produce
    min_verify_trades: int = 30       # holdout: fewest trades needed before an edge counts as verified
    # Bernoulli gate and sizing (see bernoulli.py)
    min_p_above_breakeven: float = 0.9   # act needs this much posterior mass above breakeven for the bin
    kelly_multiplier: float = 0.25
    kelly_cap: float = 0.02
    tuned: bool = False

    def bernoulli(self):
        from .bernoulli import BernoulliConfig
        return BernoulliConfig(min_p_above_breakeven=self.min_p_above_breakeven,
                               kelly_multiplier=self.kelly_multiplier, kelly_cap=self.kelly_cap)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "PolicyConfig":
        d = d or {}
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def breakeven(sl: float, tp: float, cost=0.0):
    return (sl + cost) / (sl + tp)


def expected_r(p, sl: float, tp: float, cost=0.0):
    return p * tp / sl - (1.0 - p) - cost / sl


def tiers(P: np.ndarray, sl: float, tp: float, cost, cfg: PolicyConfig) -> dict:
    """Vectorised policy. `P` is (n, 3) in ACTIONS order; `cost` in pips, scalar or per row."""
    P = np.atleast_2d(np.asarray(P, float))
    cost = np.broadcast_to(np.asarray(cost, float), (len(P),))
    side = np.where(P[:, BUY] >= P[:, SELL], BUY, SELL)
    p = P[np.arange(len(P)), side]
    be = breakeven(sl, tp, cost)
    edge = p - be
    conf = np.clip((P.max(axis=1) - 1 / 3) / (2 / 3), 0.0, 1.0)
    most_likely = P.argmax(axis=1) == side
    act = most_likely & (edge >= cfg.act_margin) & (conf >= cfg.act_confidence)
    confirm = (edge >= cfg.confirm_margin) & (conf >= cfg.confirm_confidence)
    tier = np.where(act, 2, np.where(confirm, 1, 0))
    return {"side": side, "p": p, "breakeven": be, "edge": edge, "confidence": conf, "tier": tier}


def non_overlapping(mask: np.ndarray, sym: np.ndarray, t: np.ndarray, t_end: np.ndarray) -> np.ndarray:
    """One position per symbol at a time: a signal is taken only when the previous trade on that symbol has
    reached the end of its label horizon. Rows must be sorted by time. Keeps the trades close to independent,
    which the Wilson interval assumes."""
    keep = np.zeros(len(mask), bool)
    busy_until: dict = {}
    for i in np.flatnonzero(mask):
        s = sym[i]
        if s not in busy_until or t[i] >= busy_until[s]:
            keep[i] = True
            busy_until[s] = t_end[i]
    return keep


def trade_stats(taken: np.ndarray, side: np.ndarray, y: np.ndarray, r_buy: np.ndarray, r_sell: np.ndarray,
                be: np.ndarray) -> dict:
    """Realised result of the trades in `taken`: win = the chosen side reached its target before its stop."""
    n = int(taken.sum())
    if n == 0:
        return {"n": 0, "wins": 0, "win_rate": None, "wilson_95": [None, None], "breakeven_rate": None,
                "mean_r": None, "total_r": 0.0, "buys": 0, "sells": 0}
    s, yy = side[taken], y[taken]
    r = np.where(s == BUY, r_buy[taken], r_sell[taken])
    wins = int((s == yy).sum())
    lo, hi = wilson(wins, n)
    return {"n": n, "wins": wins, "win_rate": _f(wins / n), "wilson_95": [_f(lo), _f(hi)],
            "breakeven_rate": _f(be[taken].mean()), "mean_r": _f(r.mean()), "total_r": _f(r.sum()),
            "buys": int((s == BUY).sum()), "sells": int((s == SELL).sum())}


def evaluate(P, y, r_buy, r_sell, sym, t, t_end, sl, tp, cost, cfg: PolicyConfig) -> dict:
    """What the policy would have done on these rows. `trades` are non-overlapping (one position per symbol
    at a time); `signals` counts every signalled candle and is reported for context only."""
    tr = tiers(P, sl, tp, cost, cfg)
    act, signal = tr["tier"] == 2, tr["tier"] >= 1
    args = (tr["side"], y, r_buy, r_sell, tr["breakeven"])
    return {
        "act": trade_stats(non_overlapping(act, sym, t, t_end), *args),
        "act_or_confirm": trade_stats(non_overlapping(signal, sym, t, t_end), *args),
        "signals": {"rows": int(len(y)), "act": int(act.sum()), "confirm": int((tr["tier"] == 1).sum()),
                    "hold": int((tr["tier"] == 0).sum())},
    }


ACT_MARGINS = tuple(round(x, 3) for x in np.arange(0.0, 0.2001, 0.02))
ACT_CONFIDENCES = tuple(round(x, 3) for x in np.arange(0.0, 0.5001, 0.05))


def tune(P, y, r_buy, r_sell, sym, t, t_end, sl, tp, cost, cfg: PolicyConfig) -> tuple[PolicyConfig, dict]:
    """Pick (act_margin, act_confidence) maximising the mean realised R per validation trade, subject to at
    least `cfg.min_trades` non-overlapping trades. Falls back to the defaults (tuned=False) when no pair
    qualifies or the best one does not make money: thresholds are never tuned towards a losing policy."""
    best, tried = None, 0
    for m in ACT_MARGINS:
        for c in ACT_CONFIDENCES:
            cand = replace(cfg, act_margin=m, act_confidence=c)
            tr = tiers(P, sl, tp, cost, cand)
            if int((tr["tier"] == 2).sum()) < cfg.min_trades:
                continue
            st = trade_stats(non_overlapping(tr["tier"] == 2, sym, t, t_end), tr["side"], y, r_buy, r_sell,
                             tr["breakeven"])
            tried += 1
            if st["n"] < cfg.min_trades:
                continue
            key = (st["mean_r"], st["n"])
            if best is None or key > best[0]:
                best = (key, cand, st)
    info = {"objective": "mean realised R per non-overlapping validation trade", "min_trades": cfg.min_trades,
            "grid": {"act_margin": list(ACT_MARGINS), "act_confidence": list(ACT_CONFIDENCES)},
            "candidates_with_enough_trades": tried, "n_validation": int(len(y))}
    if best is None:
        return replace(cfg, tuned=False), {**info, "tuned": False,
                                           "reason": f"no threshold pair produced {cfg.min_trades} validation "
                                                     f"trades; defaults kept"}
    (mean_r, _), cand, st = best
    if mean_r is None or mean_r <= 0:
        return replace(cfg, tuned=False), {**info, "tuned": False, "best_validation": st,
                                           "reason": "no threshold pair made money on validation; defaults kept"}
    return replace(cand, tuned=True), {**info, "tuned": True, "act_margin": cand.act_margin,
                                       "act_confidence": cand.act_confidence, "validation": st}


CONFIRM_P_VALUE = 0.10   # pooled: signals must beat breakeven at this one-sided level to be shown at all


def verify(has_skill: bool, holdout: dict, cfg: PolicyConfig, pooled: bool = True) -> dict:
    """The strongest tier the holdout evidence allows. `holdout` is the output of `evaluate` on the holdout.

    pooled (all instruments together) - the evidence a signal needs to be shown at all:
      hold     no skill over the base rate, or too few signals to judge, or their win rate is not
               distinguishable from breakeven (exact one-sided binomial test, p > 0.10), or they lost money
      confirm  the signals beat breakeven at that level
      act      and the act-tier trades have a 95% Wilson lower bound above breakeven
    one instrument (pooled=False) - a veto on top of the pooled verdict, lenient where its own data is thin:
      hold     no skill there, or its signals lost money over enough trades
      confirm  too few act-tier trades there to verify, or not above breakeven with 95% confidence
    """
    if not has_skill:
        return {"max_tier": "hold", "reason": "the holdout shows no skill over the base rate; every decision "
                                              "is hold until a retrain shows skill"}
    sig, act = holdout["act_or_confirm"], holdout["act"]
    k = cfg.min_verify_trades
    if sig["n"] >= k and (sig["mean_r"] or 0) <= 0:
        return {"max_tier": "hold", "reason": f"the model beats the base rate but its signals did not make "
                                              f"money on the holdout (mean R {sig['mean_r']} over {sig['n']} "
                                              f"trades); every decision is hold"}
    if pooled:
        if sig["n"] < k:
            return {"max_tier": "hold", "reason": f"the model beats the base rate but gave only {sig['n']} "
                                                  f"signals on the holdout (need {k}) so no edge can be "
                                                  f"shown; every decision is hold"}
        from .bernoulli import binomial_test
        pv = binomial_test(sig["wins"], sig["n"], sig["breakeven_rate"])
        if pv > CONFIRM_P_VALUE:
            return {"max_tier": "hold", "p_value": round(pv, 6),
                    "reason": f"the model beats the base rate (it knows when price will move) but the win rate "
                              f"of its signals on the holdout, {sig['win_rate']} over {sig['n']} trades, is not "
                              f"distinguishable from breakeven {sig['breakeven_rate']} (one-sided p = {pv:.3f}); "
                              f"every decision is hold"}
    if act["n"] >= k and (act["mean_r"] or 0) > 0 and act["wilson_95"][0] > act["breakeven_rate"]:
        return {"max_tier": "act", "reason": f"holdout win rate {act['win_rate']} over {act['n']} trades; 95% "
                                             f"lower bound {act['wilson_95'][0]} is above breakeven "
                                             f"{act['breakeven_rate']}"}
    if act["n"] < k:
        why = f"only {act['n']} act-tier trades on the holdout (need {k}) so the edge is not verified"
    else:
        why = (f"holdout win rate {act['win_rate']} over {act['n']} trades is not above breakeven "
               f"{act['breakeven_rate']} with 95% confidence (lower bound {act['wilson_95'][0]}, mean R "
               f"{act['mean_r']})")
    return {"max_tier": "confirm", "reason": why + "; decisions are capped at confirm"}


def decide(p_buy: float, p_sell: float, p_hold: float, sl: float, tp: float, cost: float, cfg: PolicyConfig,
           max_tier: str = "act") -> dict:
    """One decision. `max_tier` is the cap from `verify` ("hold" forces a hold)."""
    tr = tiers(np.array([[p_buy, p_sell, p_hold]]), sl, tp, cost, cfg)
    side, tier = int(tr["side"][0]), TIERS[int(tr["tier"][0])]
    raw_tier = tier
    if TIERS.index(tier) > TIERS.index(max_tier):
        tier = max_tier
    return {
        "action": ACTIONS[side] if tier != "hold" else "hold", "tier": tier, "lean": ACTIONS[side],
        "uncapped_tier": raw_tier, "confidence": round(float(tr["confidence"][0]), 6),
        "breakeven_probability": round(float(tr["breakeven"][0]), 6), "edge": round(float(tr["edge"][0]), 6),
        "expected_r": {"buy": round(float(expected_r(p_buy, sl, tp, cost)), 6),
                       "sell": round(float(expected_r(p_sell, sl, tp, cost)), 6)},
    }


# ----------------------------------------------------------------------------- composite score

@dataclass(frozen=True)
class CompositeConfig:
    """Weights of the composite score (the reference's composite-scoring pattern: atomic calibrated answers,
    weights in code). The composite is for reading and ranking; it never decides the action.

    A component is a yes-probability that argues for one side:
      tp1 / tp2   P(that side reaches target 1 / target 2 before its stop)
      a pattern   its probability counts for the side named by `direction_feature` (+1 = up = buy) and
                  1 - probability for the other side; labels in `against` argue against that direction
                  (a likely fakeout of an upward break supports the sell side).
    Labels without a weight are reported but not scored."""
    weights: dict = field(default_factory=lambda: {
        "tp1": 0.40, "tp2": 0.10, "cont_after_pullback": 0.15, "ht1_cont_after_pullback": 0.15,
        "retest_then_continue": 0.10, "fakeout": 0.10})
    direction_feature: dict = field(default_factory=lambda: {
        "cont_after_pullback": "tr_pullback_dir", "ht1_cont_after_pullback": "ht1_pullback_dir",
        "retest_then_continue": "tr_brk_dir", "fakeout": "tr_brk_dir"})
    against: tuple = ("fakeout",)

    def to_dict(self) -> dict:
        return asdict(self)


def composite(nouls: dict, directions: dict, cfg: CompositeConfig = CompositeConfig()) -> dict:
    """`nouls`: label -> yes-probability of every applicable noul; `directions`: label -> +1 / -1 (the side
    the pattern points to), for the directional labels."""
    parts = []
    for tag, long_, short_ in (("tp1", "long_tp1", "short_tp1"), ("tp2", "long_tp2", "short_tp2")):
        if long_ in nouls and short_ in nouls and cfg.weights.get(tag):
            parts.append({"component": tag, "weight": cfg.weights[tag], "buy": nouls[long_], "sell": nouls[short_],
                          "from": [long_, short_]})
    unscored = []
    for label, q in nouls.items():
        if label in ("long_tp1", "short_tp1", "long_tp2", "short_tp2"):
            continue
        d, w = directions.get(label), cfg.weights.get(label)
        if not w or not d:
            unscored.append(label)
            continue
        up = (d > 0) != (label in cfg.against)
        parts.append({"component": label, "weight": w, "buy": q if up else 1 - q, "sell": 1 - q if up else q,
                      "from": [label], "points": "up" if d > 0 else "down"})
    total = sum(p["weight"] for p in parts)
    if not total:
        return {"buy": None, "sell": None, "weights": {}, "components": [], "unscored": unscored,
                "configured_weights": dict(cfg.weights)}
    for p in parts:
        p["weight"] = round(p["weight"] / total, 4)
        p["buy"], p["sell"] = round(float(p["buy"]), 6), round(float(p["sell"]), 6)
    return {"buy": round(sum(p["weight"] * p["buy"] for p in parts), 6),
            "sell": round(sum(p["weight"] * p["sell"] for p in parts), 6),
            "weights": {p["component"]: p["weight"] for p in parts}, "components": parts, "unscored": unscored,
            "configured_weights": dict(cfg.weights),
            "note": "weighted mean of the applicable components (weights renormalised over those present); "
                    "a reading aid and a ranking key, not a probability"}
