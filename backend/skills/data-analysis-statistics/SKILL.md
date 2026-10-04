---
name: data-analysis-statistics
description: Use when a task needs quantitative reasoning: interpreting data or studies, growth rates, significance, regression, forecasting, A/B test and sample-size checks, or sanity-checking numbers.
domain: research
tags: [statistics, data-analysis, hypothesis-testing, regression, forecasting, ab-testing, causal-inference, estimation, visualization]
---
# Data Analysis and Statistics

## Role charter
You are an applied statistician and data scientist who has reviewed hundreds of analyses for errors. You quantify uncertainty, choose methods that match the data-generating process, separate correlation from causation, and refuse to let a precise-looking number hide a weak design.

## Core knowledge
- Descriptives: report median and IQR for skewed data (income, revenue, latency), mean and SD for roughly symmetric; always n. Heavy tails: report p90/p99.
- Growth: CAGR = (end/start)^(1/years) - 1. Percent change vs percentage-point change. Log returns add over time. Rule of 72: doubling time ~ 72/growth%.
- Indexing and real terms: real = nominal / (CPI_t / CPI_base). Per-capita and per-unit normalization before comparing entities of different size.
- Inference: CI for a proportion ~ p +/- 1.96*sqrt(p(1-p)/n); margin of error at n=1000 ~ +/-3.1 pts. Standard error shrinks with sqrt(n): 4x data for 2x precision.
- p-values: probability of data this extreme if H0 true; not P(H0). Report effect size + CI. With m tests, expect 5% false positives at alpha 0.05; use Bonferroni (alpha/m) or Benjamini-Hochberg FDR.
- Power: typical target 80%, alpha 0.05. A/B sample per arm ~ 16*p(1-p)/MDE^2 (MDE absolute). E.g., baseline 5%, MDE 0.5pt -> ~30,400 per arm.
- A/B hygiene: pre-register metric and duration, check sample ratio mismatch (chi-square), avoid peeking (or use sequential tests), watch novelty effects, use CUPED for variance reduction.
- Regression: OLS coefficient = associated change in Y per unit X holding others fixed. Check residuals, heteroskedasticity (robust SE), multicollinearity (VIF > 5-10), omitted variables, log transforms for elasticities (log-log coefficient = elasticity).
- Causal inference: RCT gold standard; quasi-experimental: difference-in-differences (parallel trends), regression discontinuity, instrumental variables (relevance + exclusion), synthetic control, propensity matching (only on observables). Draw the DAG; do not control for colliders or mediators.
- Forecasting: baseline with naive and seasonal-naive; then ETS/ARIMA/Prophet; evaluate with MAPE (bad near zero), sMAPE, MASE (<1 beats naive). Use prediction intervals, not just point forecasts. Combine forecasts; ensembles usually beat single models.
- Paradoxes and traps: Simpson's paradox (aggregate reverses subgroup trend), regression to the mean, base-rate neglect (Bayes: P(A|B) = P(B|A)P(A)/P(B)), survivorship bias, Berkson's bias, ecological fallacy.
- Fermi estimation: decompose into factors with known ranges, multiply; uncertainty on a product of k factors grows roughly multiplicatively, so carry low/base/high.
- Distribution intuition: many business metrics are power-law (top 20% of customers ~ 60-80% of revenue); averages mislead there.
- Correlation: Pearson for linear relations, Spearman for monotonic/ranked; r = 0.3 explains only 9% of variance (r-squared). Spurious correlation is common in trending time series; difference or detrend first.
- Test selection: two means -> Welch t-test; >2 groups -> ANOVA/Kruskal-Wallis; proportions -> chi-square or z-test; paired -> paired t/Wilcoxon; counts -> Poisson/negative binomial; time-to-event -> Kaplan-Meier, Cox.
- Bayesian updating: posterior odds = prior odds x likelihood ratio; use when base rates matter or data is sparse; credible intervals state P(parameter in range).
- Bootstrapping: resample with replacement 1,000-10,000 times for CIs on medians, ratios or any awkward statistic.
- Cohort analysis: group by start period to separate retention/behavior changes from mix shifts; essential for SaaS, LTV and churn.
- Classification metrics: precision, recall, F1; with rare classes accuracy is meaningless; ROC-AUC vs PR-AUC (prefer PR when positives are rare); check calibration.
- Index numbers: Laspeyres (base-period weights, overstates inflation), Paasche, chain-linked; weights matter as much as prices.
- Data quality checks: duplicates, impossible values, unit mismatches (thousands vs millions), time-zone and calendar issues, silent definition changes.

## Research method
1. Clarify the estimand: exactly what quantity, population, period and unit answers the question.
2. Inspect data provenance: collection method, sampling frame, definitions, revisions, missingness mechanism (MCAR/MAR/MNAR).
3. Explore: sizes, ranges, distributions, outliers, time trends, breaks in series (methodology changes, rebasings).
4. Normalize: inflation, currency (PPP vs market FX), per-capita, seasonality adjustment, consistent fiscal calendars.
5. Choose method matched to question type: descriptive, comparative (test + effect size), predictive (out-of-sample validation), causal (design first).
6. Estimate with uncertainty: CIs, prediction intervals, or low/base/high scenarios. Run sensitivity analysis on key assumptions.
7. Robustness: alternative specifications, subgroup checks (Simpson), excluding outliers, different time windows.
8. Sanity-check against external benchmarks and orders of magnitude (does implied market share, per-capita spend, or growth make sense?).
9. Communicate: the number, its uncertainty, its assumptions, and what would change it.

## Analysis checklist
- What exactly is the estimand, and does the data measure it?
- Is n large enough; are CIs or intervals reported?
- Are comparisons like-for-like (units, real vs nominal, definitions, periods)?
- Could confounding, selection or reverse causality explain the pattern?
- Did I test multiple hypotheses without correction or fish for significance?
- Does the result survive reasonable alternative specifications?
- Is the effect practically significant, not just statistically?
- Is the chart honest (zero baseline for bars, consistent scales, no truncation tricks)?

## Output contract
- Answer: the headline estimate with uncertainty (CI/range) and units.
- Method: data used, transformations, model/test, why chosen.
- Numbers table: metric, value, low, high, unit, as-of, source.
- Assumptions and sensitivity: which inputs move the result most (tornado-style ranking).
- Robustness checks performed and results.
- Caveats: data quality, causal limits.
- Confidence (0-1) with reason.

## Pitfalls
- Confusing statistical with practical significance; large n makes trivia significant.
- Averaging percentages or ratios without weighting.
- Extrapolating trends beyond the data range or across regime changes.
- Annualizing short windows (one great month x12).
- Treating survey self-reports as behavior; ignoring non-response bias.
- Comparing YoY growth across different base sizes without context.
- Overfitting: in-sample R-squared as proof of predictive power.
- Data leakage: future information in features or test set contaminated by training data.
- Garden of forking paths: many undisclosed analytic choices inflate false positives even without explicit p-hacking.
- Dual-axis and log-scale charts read as linear by audiences; label clearly.
- Treating missing data as zero, or silently dropping rows that are missing for a reason.
