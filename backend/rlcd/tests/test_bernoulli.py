import math

import numpy as np
import pytest
from scipy import stats

from rlcd import bernoulli as B


def test_kelly_and_expected_r_on_worked_numbers():
    assert B.kelly(0.4, 2.5) == pytest.approx(0.16)                 # 0.4 - 0.6 / 2.5
    assert B.expected_r(0.4, 2.5) == pytest.approx(0.4)             # 0.4*2.5 - 0.6
    assert B.expected_r(0.4, 2.5, cost=0.05) == pytest.approx(0.35)
    assert B.variance_r(0.4, 2.5) == pytest.approx(0.4 * 0.6 * 3.5 ** 2)
    assert B.breakeven(2.5) == pytest.approx(1 / 3.5)
    assert B.expected_r(B.breakeven(2.5, 0.1), 2.5, 0.1) == pytest.approx(0.0)
    assert B.kelly(0.2, 2.5) == 0.0                                 # no edge, no stake
    assert B.kelly(B.breakeven(2.5), 2.5) == pytest.approx(0.0)


def test_sizing_is_quarter_kelly_capped():
    cfg = B.BernoulliConfig(kelly_cap=0.02)
    s = B.sizing(0.4, 2.5, cfg)
    assert s["kelly"] == pytest.approx(0.16) and s["kelly_quarter"] == pytest.approx(0.02) and s["capped"]
    s = B.sizing(0.31, 2.5, cfg)                                    # f* = 0.034 -> quarter = 0.0085, under the cap
    assert s["kelly_quarter"] == pytest.approx(0.034 / 4, abs=1e-6) and not s["capped"]
    assert s["log_growth_per_trade"] == pytest.approx(
        0.31 * math.log(1 + 0.0085 * 2.5) + 0.69 * math.log(1 - 0.0085), abs=1e-7)
    # growth is maximal at full Kelly and negative far beyond it
    assert B.log_growth(0.4, 2.5, 0.16) > B.log_growth(0.4, 2.5, 0.08) > 0 > B.log_growth(0.4, 2.5, 0.5)


def test_beta_update_arithmetic():
    cfg = B.BernoulliConfig(prior_alpha=1, prior_beta=1)
    p = B.posterior(7, 3, 0.3, cfg)
    assert (p["alpha"], p["beta"], p["n"]) == (8.0, 4.0, 10)
    assert p["mean"] == pytest.approx(8 / 12)
    assert p["p_above_breakeven"] == pytest.approx(stats.beta.sf(0.3, 8, 4), abs=1e-6)
    lo, hi = p["ci95"]
    assert lo < p["mean"] < hi and stats.beta.cdf(hi, 8, 4) - stats.beta.cdf(lo, 8, 4) == pytest.approx(0.95, abs=1e-4)
    empty = B.posterior(0, 0, 0.3, cfg)                             # uniform prior: 70% of the mass above 0.3
    assert empty["p_above_breakeven"] == pytest.approx(0.7) and empty["n"] == 0


def test_p_above_breakeven_is_monotonic_in_wins():
    ps = [B.posterior(w, 20 - w, 0.3)["p_above_breakeven"] for w in range(21)]
    assert all(b >= a for a, b in zip(ps, ps[1:])) and ps[0] < 0.01 and ps[-1] > 0.99
    more = [B.posterior(4 * k, 6 * k, 0.3)["p_above_breakeven"] for k in (1, 5, 25)]   # 40% wins, more trials
    assert more[0] < more[1] < more[2]


def test_binomial_p_value():
    assert B.binomial_test(3, 3, 0.5) == pytest.approx(0.125)       # by hand: 0.5^3
    assert B.binomial_test(2, 3, 0.5) == pytest.approx(0.5)         # P(X>=2) = 3/8 + 1/8
    assert B.binomial_test(45, 100, 0.3) == pytest.approx(
        stats.binomtest(45, 100, 0.3, alternative="greater").pvalue)
    assert B.binomial_test(0, 10, 0.3) == pytest.approx(1.0)
    assert B.binomial_test(0, 0, 0.3) is None


def test_trades_needed_formula():
    za, zb = stats.norm.ppf(0.95), stats.norm.ppf(0.8)
    want = ((za * math.sqrt(0.3 * 0.7) + zb * math.sqrt(0.4 * 0.6)) / 0.1) ** 2
    assert B.trades_needed(0.4, 0.3) == math.ceil(want) == 136
    assert B.trades_needed(0.32, 0.3) > B.trades_needed(0.4, 0.3) > B.trades_needed(0.6, 0.3)   # LLN
    assert B.trades_needed(0.3, 0.3) is None and B.trades_needed(0.2, 0.3) is None


def test_ledger_counts_each_decision_once(tmp_path):
    led = B.Ledger(tmp_path / "bernoulli.json")
    d = {"decision_id": "a", "probabilities": {"buy": 0.47, "sell": 0.12, "hold": 0.41}, "action": "buy",
         "symbol": "XAUUSD", "session": "london"}
    cells = led.update("setup:15m", d, "buy")
    assert cells == {"buy:bin4": [1, 0], "sell:bin1": [0, 1], "XAUUSD|london": [1, 0]}
    assert led.update("setup:15m", d, "buy") is None                # same decision_id: no double count
    led.update("setup:15m", {**d, "decision_id": "b", "action": "hold"}, "sell")
    t = led.table("setup:15m")
    assert t["bins"]["buy"][4] == [1, 1] and t["bins"]["sell"][1] == [1, 1]
    assert t["symbol_session"] == {"XAUUSD|london": [1, 0]}         # a hold is not a trade
    assert t["online_trials"] == 2


def test_thompson_draws_come_from_the_posterior():
    rng = np.random.default_rng(0)
    draws = [B.thompson(30, 10, rng) for _ in range(4000)]
    assert np.mean(draws) == pytest.approx(31 / 42, abs=0.01)
