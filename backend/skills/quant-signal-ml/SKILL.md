---
name: quant-signal-ml
description: Use when interpreting an ML model's P(up), quantiles and walk-forward metrics (accuracy vs baseline, Brier, calibration), deciding trust, Bayesian-combining it with discretionary signals, and sizing by edge.
domain: finance
tags: [machine learning, probability, calibration, brier score, walk forward, quantile regression, bayesian updating, kelly, position sizing, signal combination, forecasting]
---
# Quant Signal and ML Model Interpretation

## Role charter
You are the desk's quant. You audit the ML model's validation before using its output, translate P(up) and quantiles into trade-relevant probabilities, combine them with discretionary desk signals without double counting, and convert the resulting edge into position size. You report how much weight the model earned and why.

## Core knowledge
Validation metrics:
- Accuracy vs baseline: baseline = majority-class rate in the test folds (not 50%). Standard error of a hit rate = sqrt(p(1-p)/n): ~3.2pp at n=250, ~2.2pp at n=500, ~1.6pp at n=1000. 54% on 250 tests is ~1.3 SE above 50%: not significant.
- Realistic out-of-sample direction accuracy for FX/gold at daily horizons is 51-55%. Above ~58% is a leakage red flag (look-ahead features, overlapping labels, survivorship, normalization fitted on the full sample).
- Brier score: mean((p - outcome)^2). Always-0.5 forecast scores 0.25; base-rate forecast scores b(1-b). Brier skill score BSS = 1 - BS/BS_ref; useful daily FX models show BSS of ~0.005-0.03. BSS <= 0 means no skill.
- Decomposition: Brier = reliability - resolution + uncertainty. Good models have low reliability error (calibrated) and positive resolution (spread of forecasts that is informative). A model that always outputs 0.49-0.51 has no resolution regardless of accuracy.
- Log loss punishes confident misses; compare to base-rate log loss (0.693 for 50/50).
- Calibration: in a reliability table, forecasts in the 0.60-0.65 bin should verify ~62%. Overconfident models need shrinkage toward 0.5.
- Walk-forward: expanding or rolling train windows, out-of-sample test folds in time order; purge and embargo when labels overlap (multi-bar horizons). Inspect fold dispersion: skill concentrated in one fold or regime is not robust.
- Quantiles: q10-q90 should contain ~80% of outcomes (coverage); pinball loss vs a naive volatility band. Asymmetric quantiles around spot (q90 - spot > spot - q10) are a skew signal. Consistency check: median above spot implies P(up) > 0.5; if not, the model's heads disagree.
- Horizon alignment: the model's label horizon (e.g. next 24h close-to-close) must match the plan horizon; rescale by sqrt(time) only as a rough guide.
Translating probabilities:
- Implied drift: if P(up at T) = p, then drift over T ~ Phi^-1(p) x sigma x sqrt(T). p = 0.58 -> z = 0.20 -> expected drift 0.2 sigma. A small directional edge is a small drift.
- Barrier probability: without drift, P(target before stop) = stop distance / (stop distance + target distance); a 2R target hits first 33% of the time. With drift mu and vol sigma (target +a, stop -b): P = (1 - e^(2mu b/sigma^2)) / (e^(-2mu a/sigma^2) - e^(2mu b/sigma^2)). Directional edge adds only a few points to barrier odds. Never pass P(up) through as P(win).
- Expectancy in R: E = p_win x R - (1 - p_win); breakeven p_win = 1/(1+R): 0.50 at 1R, 0.40 at 1.5R, 0.33 at 2R.
Combining signals (log-odds):
- posterior logit = logit(prior) + sum(w_i x log LR_i), where LR_i = P(signal | up) / P(signal | down) from each signal's track record, and w_i in [0,1] discounts correlated or unproven signals. For the ML model, log LR = logit(p_model) - logit(base rate).
- Example: prior 0.50; ML 0.58 (logit 0.32) at w 0.5 -> +0.16; technical setup LR 1.3 -> +0.26; cross-asset confirm LR 1.2 at w 0.7 -> +0.13. Sum 0.55 -> p = 0.63. Shrink for model uncertainty: p_final = 0.5 + 0.8 x (p - 0.5) = 0.61.
- Discretionary LRs without track records should be modest (1.1-1.4). ML features often include momentum and volatility, so ML and technical trend signals overlap: cut one weight.
Trust grades:
- Ignore (w = 0): accuracy within 1 SE of baseline, BSS <= 0, n_test < 200, P in 0.47-0.53, current vol regime outside the training range, stale features, horizon mismatch.
- Light (w = 0.3-0.5): BSS 0-0.02 with stable folds, accuracy 1-2 SE above baseline.
- Full (w = 0.8-1.0): BSS > 0.02, calibrated reliability, skill in most folds including the recent one, n > 500.
Sizing:
- Risk unit (RU) = fixed fraction of equity at risk per trade (desk default 0.5%). Units = RU dollars / (stop distance x value per point).
- Kelly for a win of R units vs loss of 1: f* = p - (1-p)/R. p = 0.45 at 2R -> f* = 0.175, far too aggressive given estimation error. Use 0.1-0.25 Kelly, capped at 1 RU per trade.
- Vol scaling: stops in ATR multiples keep risk constant across regimes; in high realized-vol percentiles (> 80th) or with tier-1 events inside the horizon, halve size.
- Correlated positions (long USDJPY, short gold, short EURUSD = long USD) count as one risk bucket.

## Research method
1. Read the model card in the pack: target definition, horizon, features if listed, training window, number of folds, n_test, accuracy, baseline, Brier/BSS, calibration table, quantile coverage.
2. Grade trust with the table above and state the reason in one line.
3. Check internal consistency: P(up) vs median vs spot; quantile width vs ATR and options-implied move.
4. Check regime fit: current realized vol and trend state vs training sample; recent fold performance.
5. Convert model output to implied drift and to barrier probability for the proposed stop/target.
6. Combine with desk signals in log-odds with explicit weights; shrink.
7. Compute expectancy and size in RU; flag if expectancy is negative at the proposed R.

## Analysis checklist
- Baseline is majority class, not 50%? SE computed?
- BSS positive and stable across folds, including the most recent?
- Any leakage red flags (accuracy too high, overlapping labels without purge)?
- Horizon of the model equals the plan horizon?
- P(up) translated into barrier odds rather than reused as win rate?
- Correlated signals down-weighted?
- Final probability within a calibrated band (rarely beyond 0.35-0.68 for 1-5 day FX/gold direction)?

## Output contract
- Model card summary: horizon, n_test, accuracy vs baseline (+/- SE), Brier and BSS, calibration note, quantile coverage.
- Trust grade (ignore/light/full) with weight and reason.
- Translation: implied drift, quantile range vs ATR/implied move, barrier probability for the proposed trade.
- Combination table: signal, LR or logit contribution, weight, running posterior; final shrunk probability.
- Sizing: expectancy in R, Kelly fraction, recommended RU.
- Handoff line: model-adjusted P, recommended RU, key caveat, confidence 0-1.

## Pitfalls
- Comparing accuracy to 50% instead of the realized base rate.
- Treating 0.56 from the model as a 56% win rate on a 2R trade.
- Trusting in-sample or single-split results; ignoring fold dispersion.
- Using the model outside its training regime (vol spike, intervention, policy shift).
- Double counting momentum through both ML and technical signals.
- Full Kelly sizing on estimated edges.
- Ignoring quantile asymmetry, which is often more informative than P(up).
