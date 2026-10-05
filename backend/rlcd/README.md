# RLCD — Reinforcement Learning Calibrated Decision

A small decision model with an HTTP API. It answers typed questions (yes/no, one of N, an ordered score)
with **calibrated probabilities** instead of text, and turns those probabilities into day-trade decisions
(`buy` / `sell` / `hold`) through an explicit policy. Analysis tool, not financial advice.

```
cd backend/rlcd
uv run pytest -q                                                   # 80 tests, no network
uv run uvicorn rlcd.api:app --host 127.0.0.1 --port 8095           # the service
curl -X POST localhost:8095/v1/train -d '{}' -H 'content-type: application/json'   # train everything
```

## What it is, and what it is not

The wire contract follows Typesafe's "System One" documentation (one evaluation endpoint, three question
types, the same confidence formulas) and the calibration is scikit-learn's `CalibratedClassifierCV`.

**The reference does not publish how its RLCD model is trained, so the training here is our own design**,
and it is deliberately modest:

* It is a **contextual bandit**. Context = features of the chart at a candle close, actions = buy / sell /
  hold, reward = the realised outcome (which side reached its target before its stop). Because both
  directions can be simulated on history, **every row carries the reward of every action**, so nothing has to
  be explored to learn: we fit outcome models, calibrate them, act only when the calibrated expected reward
  clears a gate, and fold real scored outcomes back in through `/v1/feedback`.
* The "reinforcement" is exactly two things: (1) every scored outcome posted with a `decision_id` updates
  Beta-Bernoulli win/loss counts immediately, and those counts gate `act`; (2) feedback rows become extra,
  up-weighted training rows at the next `/v1/train`. There is no learned controller, no sequential credit
  assignment and no exploration unless you ask for it (`explore: true`, Thompson sampling, labelled as such).
* There is **no language model**. A question is answered only when it is bound to a trained head. Free-form
  `instructions` are accepted and ignored.
* A probability of 0.8 is supposed to come true about 80% of the time. Whether it does is measured on rows the
  model never saw and published at `/v1/calibration`. When that report shows no skill over the base rate, every
  decision is `hold` and says why. Noise is never dressed up as a signal.

## Heads

| head | type | answers | trained on |
|---|---|---|---|
| `intent` | choice: `position`, `research`, `other` | what is the user asking for | seed phrasings (templates) + feedback |
| `trade_style` | choice: `daytrade`, `swing` | which style a position request implies (gate on `intent` first) | the position seeds + feedback |
| `setup:5m`, `setup:15m`, `setup:1h` | choice: `buy`, `sell`, `hold` | from this candle's close, which action reaches its target before its stop | quant sidecar rows, pooled over symbols and pip sizes |
| `long_tp1:<iv>`, `short_tp1:<iv>` | noul | P(buy), P(sell) of the same setup model | — (derived) |
| `<label>:<iv>` | noul | one head per extra outcome label the sidecar exports (`meta.label_names`), e.g. `fakeout:15m` | only the rows where that label exists |

Pattern heads (`<label>:<iv>`) are discovered from the data, not hard-coded: a label the sidecar adds later
trains by itself. They only answer when their pattern is on the chart; otherwise the answer is
`{"type":"noul","noul":null,"applicable":false,...}`. Applicability comes from the sidecar's `applicable` map
when it sends one, else from a gate model trained to reproduce where the label existed.

Models: intent heads are TF-IDF (word 1-2 grams + char 3-5 grams) + meta features -> logistic regression in
`CalibratedClassifierCV(method="sigmoid", cv=5)`. Setup and pattern heads are LightGBM (chosen for its exact
per-feature contributions, which `reasons` is built from) in `CalibratedClassifierCV(ensemble=True)` over
time-ordered folds; `sigmoid` unless every class has at least 3000 rows in every calibration fold, then
`isotonic`. No class weights and no resampling anywhere, so probabilities stay frequencies.

## API

Default address `127.0.0.1:8095`. Errors are `{"error": "...", "kind": "..."}`: 401 (only when `RLCD_API_KEY`
is set), 422 validation, 503 quant sidecar unreachable, 409 a training job is already running. 429 and 529
of the reference do not occur.

### `POST /v1/systemone` — the reference contract

```json
{"model": "rlcd-latest",
 "state": {"text": "xauusd m15 where entry", "attachments": 1},
 "questions": {
   "intent": {"type": "choice", "instructions": "...", "criteria": {"position": "...", "research": "...", "other": "..."}},
   "style":  {"type": "choice", "head": "trade_style"}}}
```
```json
{"model": "rlcd-0.1.2",
 "answers": {"intent": {"type": "choice", "choice": "position", "probabilities": {"position": 0.999, "research": 0.0005, "other": 0.0005},
                        "confidence": 0.9986, "head": "intent", "calibrated": true}, "style": {"...": "..."}},
 "usage": {"input_tokens": 0, "output_tokens": 0, "questions": 2, "latency_ms": 7.2}}
```

Answers: noul `{type, noul, confidence}`, choice `{type, choice, probabilities, confidence}`, score
`{type, score, legend, probabilities, confidence}` (levels indexed from 0). Limits: at most 255 choice
options, 2 to 10 score levels, 64 questions.

Where RLCD deviates from or extends the reference:

| field | where | meaning |
|---|---|---|
| `head` | question | which head answers it; defaults to the question id. Unknown head -> 422 listing the heads |
| `criteria` | question | optional for a choice (the head fixes the options); if given, its keys must equal the head's classes |
| `instructions` | question | optional, ignored |
| `model` | request | optional, defaults to `rlcd-latest` |
| `confidence` | noul answer | `|2p - 1|` (the reference documents the formula but does not return it) |
| `head`, `calibrated` | every answer | the head that answered; `false` = untrained head, the answer is the uninformed prior |
| `applicable` | pattern noul answer | `false` with `noul: null` when the pattern is not on the chart |
| `usage.questions`, `usage.latency_ms` | response | token counts are always 0 |
| `warnings` | response | present when a head is untrained or its holdout showed no skill |

Confidence: noul `|2p-1|`; choice `(p_max - 1/n)/(1 - 1/n)`; score `max(0, 1 - sum_i p_i|i-m| / MAD_unif)`.

State: intent heads take a string or `{text, attachments, timeframes, symbol}` (any subset). Setup and
pattern heads take `{x, feature_names, feature_version}` or `{symbol, interval?, as_of?, pip?}` (then
`/features` is fetched from the sidecar). A feature vector of another `feature_version` is refused with 422.
No v0.1 head is of type score; the type is validated and can be built, but binding a score question to a
choice head is a 422.

### The other endpoints

* `GET /health` — status, model version, each head trained or not, sidecar reachable.
* `GET /v1/models` — `rlcd-latest` alias + `rlcd-0.1.N` versions (N increments per successful train), heads and
  holdout metrics. Old versions stay addressable through `model`.
* `GET /v1/heads` — type, classes, expected input, trained flag, `n_train`, metrics, pending feedback rows.
* `POST /v1/rank` — `{model?, state?, candidates: [{id, state}], question, option?}` -> `candidates` sorted by
  the probability of the positive outcome (the noul, or `option` of a choice), each with `rank`, `probability`,
  `answer`. The re-ranking pattern.
* `POST /v1/decide` — the day-trade decision, see below.
* `POST /v1/scan` — `{symbols?, interval, sl_pips, tp_pips, tp2_pips, pip?: {symbol: pip}}` -> the decision for
  each symbol, ranked by expected R of its best action (ties by P(win rate > breakeven)), with its session.
  Default symbols: gold plus the USD majors the sidecar offers.
* `POST /v1/feedback` — `{decision_id?, head?, state | x + feature_version, label, weight?, source}`; appended to
  `feedback.jsonl`. With a `decision_id` of a setup decision the Bernoulli counts update at once.
* `POST /v1/train` -> 202 `{job_id}`; `GET /v1/train/{job}` -> status, progress, per-head metrics.
* `GET /v1/calibration?head=[&symbol=&pip=][&year=]` — the holdout report.
* `GET /v1/bernoulli?head=` — the posterior table and the binomial test of the holdout trades.

Patterns of the reference and where they live: speculative fan-out = many questions in one `/v1/systemone`
call, answered independently; confidence-gated routing = the `tier` of `/v1/decide` (and `confidence` on every
answer for your own gates: act / confirm / escalate, stricter for higher stakes); composite scoring = the
`composite` block (weights in `policy.CompositeConfig`); intent routing = the `intent` head plus a gate on its
confidence; re-ranking = `/v1/rank` and `/v1/scan`.

## `/v1/decide`

Request `{symbol, interval: "15m", as_of?, pip?, sl_pips: 20, tp_pips: 50, tp2_pips: 100, cost_pips?, text?,
explore?}`. It fetches `/features`, answers `setup:<interval>`, applies the policy and returns

`decision_id, model, symbol, interval, time, session, price, pip, action, tier, lean, probabilities{buy,sell,hold},
calibrated, confidence, breakeven_probability, expected_r{buy,sell}, bernoulli{...}, entry, stop, targets[tp1,tp2],
levels{buy,sell}, thresholds, max_tier, nouls{...}, not_applicable[...], composite{buy,sell,weights,...},
reasons[...], reason_groups{...}, facts, trade_style, calibration{...}, warnings[...]`.

Policy (`policy.py`, all thresholds in `PolicyConfig`, returned as `thresholds`):

```
breakeven   p* = (sl + cost) / (sl + tp)            cost = the instrument's spread in pips
expected R     = p * tp / sl - (1 - p) - cost / sl  (a non-win is counted as a full stop-out: conservative)

act      the best side is the single most likely outcome, p >= p* + act_margin, confidence >= act_confidence,
         and the Bernoulli gate passes
confirm  p >= p* + confirm_margin   (wait for the trigger candle)
hold     otherwise
```

`action` is `hold` exactly when `tier` is `hold`; `lean` is always the better side. Stop and targets are the
fixed pip distances from the entry (`stop = entry -/+ sl_pips * pip`, targets at `tp_pips` and `tp2_pips`);
they are never moved by the model. `levels` gives both sides whatever the action.

The tier is capped by evidence (`max_tier`, each cap adds a warning):

* head untrained, or holdout shows no skill over the base rate -> `hold`;
* skill, but on the pooled holdout the policy's signals lost money, or were fewer than 30, or their win rate
  is not distinguishable from breakeven (exact one-sided binomial test, p > 0.10) -> `hold`. A model can beat
  the base rate just by knowing when price will move; without direction that is not a signal;
* signals beat breakeven, but fewer than 30 act-tier holdout trades or a win rate whose 95% Wilson lower bound
  is not above breakeven -> at most `confirm`;
* the instrument (symbol + pip) itself: no skill there, or its own signals lost money over 30+ trades ->
  `hold`; too few of its own act-tier trades to verify -> at most `confirm`;
* instrument not in the training set -> at most `confirm`;
* stop/target other than the trained ones -> `hold` (the probabilities do not apply).

`warnings` also says when the stop is under half a candle's range (`sl_atr < 0.5`): at that size the outcome
is mostly intrabar noise.

`reasons` are the features that moved this prediction most (LightGBM TreeSHAP contributions to the leaning
side, averaged over the calibrated ensemble, with values). `reason_groups` is the share of the absolute
attribution by feature group (`session`, `news`, `structure`, `zones`, `strength`, `volatility`, `candle`;
from `meta.feature_groups`, or a name-based fallback for feature sets that ship none); it sums to 1.

`nouls` holds every applicable atomic answer (`long_tp1`, `short_tp1`, and each pattern head of the
interval); `composite` is their weighted mean per side with the weights it used. It is a reading aid and a
ranking key. It never decides the action.

### The Bernoulli block (`bernoulli.py`)

Each decision is a Bernoulli trial with success probability p and payoff b = tp / sl.

```
"bernoulli": {"side", "p", "payoff_b", "payoff_b_net", "cost_r", "breakeven", "expected_r", "variance_r", "std_r",
              "kelly", "kelly_quarter", "kelly_multiplier", "kelly_cap", "capped", "log_growth_per_trade",
              "stake_fraction",
              "posterior": {"alpha", "beta", "n", "mean", "ci95", "breakeven", "p_above_breakeven", "wins", "losses", "bin"},
              "symbol_session": {"key", "wins", "losses", "alpha", "beta", "mean", "ci95", "p_above_breakeven"} | null,
              "gate": {"threshold", "mode", "passed"}, "trades_needed_to_confirm"}
```

* `kelly = p - (1 - p)/b` with b net of the spread, 0 when negative; `kelly_quarter` = a quarter of it, capped
  at 2% of the account; `stake_fraction` is that number when the tier is `act` and 0 otherwise.
* `posterior`: Beta(1 + wins, 1 + losses) of the realised win rate of predictions like this one: same head,
  same side, same probability bin (10 bins). Counts start from holdout rows spaced one label horizon apart
  and grow with every outcome posted to `/v1/feedback` with a `decision_id`: no retrain. `symbol_session`
  is the same for the signals of that symbol in that session. Both restart from the new holdout at a train.
* Gate: `act` needs `p_above_breakeven >= 0.9` (`min_p_above_breakeven`). With `explore: true` a draw from
  the posterior decides instead (Thompson sampling); the response says `exploration: true`.
* `trades_needed_to_confirm`: trades needed to tell p from breakeven at 80% power (one-sided 5%).
* `/v1/bernoulli` adds the exact one-sided binomial test of the holdout trades against breakeven.

## Training

```
curl -X POST localhost:8095/v1/train -H 'content-type: application/json' -d '{}'
curl localhost:8095/v1/train/<job_id>
```

Body (all optional): `heads` (default: everything), `symbols` (default: gold + FX majors + main JPY crosses
that `/symbols` offers), `intervals` (default `5m`, `15m`, `1h`), `sl_pips: 20`, `tp_pips: 50`,
`tp2_pips: 100`, `horizon: 48`, `pips` (pip sizes per symbol; default trains gold at both 0.1 and 1.0),
`format` (`auto` | `npz` | `json`), `start`, `end`, `max_train_rows: 600000`, `max_rows` (JSON export only),
`quant_url`, `patterns: true`, `feedback_weight: 5`, `n_estimators: 300`, `seed`.

The intent heads train themselves on first start (`RLCD_AUTOTRAIN=0` to disable); the setup heads need the
sidecar and are trained by the call above. One job at a time.

How a setup head is trained (`training.py`):

1. Export each symbol from the sidecar (`format=npz` streamed to `var/datasets/`, float32 throughout; JSON
   when the sidecar has no binary export). If the pool exceeds `max_train_rows`, every k-th candle of each
   symbol is kept (neighbouring candles are near duplicates); the stride is in the metrics.
2. One cut-off instant for all symbols: the last 20% of the time span is the holdout. Rows before it whose
   label window (the next `horizon` candles of their own symbol) reaches past it are purged.
3. Model A is fitted and calibrated on the first 75% of development; the act thresholds are tuned on the
   remaining 25% (maximise mean realised R per non-overlapping trade, at least `min_trades` trades; defaults
   kept if nothing qualifies or nothing makes money). Never on the holdout.
4. Model B is fitted and calibrated on all of development with time-ordered folds (fit on the past,
   calibrate on the block after it, purge by label end) and scored once on the holdout. Model B is served.
5. Feedback rows of the head are added to development (weight `feedback_weight` x their own); holdout rows of
   the same instrument that overlap a feedback row are removed from the holdout.

## Reading the calibration report

`GET /v1/calibration?head=setup:15m`

* `log_loss` (Bernoulli log-likelihood) and `brier` next to `baseline.*`. The baseline predicts, for each row,
  the class frequencies of that row's own instrument in training. `brier_skill_score = 1 - brier/baseline`.
* `skill.has_skill`: is the Brier gain reliably positive? One-sided 95% bound across 20 contiguous time
  blocks (rows are autocorrelated, so a per-row test would be far too confident). `false` -> every decision
  is hold.
* `directional_auc`: among rows where one side won, does P(buy) - P(sell) rank buys above sells? 0.5 = none.
  A model can beat the base rate purely by knowing when the market is volatile; this number shows whether it
  also knows the direction.
* `reliability.per_class.<class>`: 10 probability bins with `mean_predicted`, `observed_rate`, `count`.
  Calibrated means the two columns agree in the bins that have rows. `ece` summarises the gap.
* `policy.holdout.act` / `act_or_confirm`: the trades the policy would have taken (one position per instrument
  at a time): `n`, `win_rate`, `wilson_95`, `breakeven_rate`, `mean_r` (the sidecar's realised R, net of the
  spread). `policy.bernoulli`: the binomial p-value against breakeven. `policy.tuning`: the tuned thresholds
  and the validation result they were chosen on. `policy.verdict`: the cap described above.
* `by_segment` (per symbol and pip) and `by_year` (per calendar year of the holdout): the same numbers, so a
  result that holds for one instrument or one period only is visible. `?symbol=XAUUSD&pip=0.1` or `?year=2026`
  returns that slice in full.
* `split`: row counts, holdout dates, purged rows, stride.

Intent reports are measured on seed phrasings whose template was not in training. They are synthetic text: a
sanity check, not accuracy on real traffic. The served intent model is then refitted on all templates.

## Data contract with the quant sidecar

`RLCD_QUANT_URL` (default `http://127.0.0.1:8090`):

* `GET /dataset?symbol=&interval=&sl_pips=&tp_pips=&tp2_pips=&horizon=&pip=[&start=&end=]&format=npz` ->
  `.npz`: `X` float32, `times` int64 epoch seconds, `long_tp1`, `short_tp1`, `long_tp2`, `short_tp2`,
  `long_r`, `short_r` float32 (NaN = unlabelled), `best` int8 (0 hold, 1 buy, 2 sell, -1 unlabelled), `meta`
  JSON (`symbol, interval, pip, params{..., spread_pips}, feature_version, feature_names, timeframes,
  label_names?, label_docs?, feature_groups?`). Without `format` the same as JSON, capped by `max_rows`.
* `GET /features?symbol=&interval=[&as_of=][&pip=]&sl_pips=&tp_pips=&tp2_pips=&horizon=` ->
  `{symbol, interval, time, price, pip, params, feature_version, feature_names, x, facts, data_note,
  applicable?, feature_docs?}`.
* `GET /symbols`.

## Configuration and files

| env | default | |
|---|---|---|
| `RLCD_HOME` | `backend/rlcd/var` | artifacts: `registry.json`, `models/<version>/*.joblib`, `feedback.jsonl`, `decisions.jsonl`, `bernoulli.json`, `datasets/*.npz` |
| `RLCD_QUANT_URL` | `http://127.0.0.1:8090` | the quant sidecar; `/v1/train` also takes `quant_url` (local hosts only unless `RLCD_ALLOW_REMOTE_QUANT=1`) |
| `RLCD_AUTOTRAIN` | `1` | train the intent heads on first start |
| `RLCD_API_KEY` | unset | when set, every call but `/health` needs `Authorization: Bearer <key>` |

Models are joblib files and are only ever loaded from inside `RLCD_HOME`.

```
rlcd/  api.py  contract.py  confidence.py  policy.py  bernoulli.py  metrics.py  training.py  store.py  quant.py
       heads/{base,intent,setup}.py   seeds/intent.py
tests/ synth.py is a synthetic sidecar (same wire contract) so no test touches the network
```

## Known limits

* Labels count a stop and a target inside one candle as the stop, and the fixed 20-pip stop is a fraction of
  one candle's range on some instruments and timeframes. Read the per-instrument report before trusting a head.
* Thresholds are tuned for the mean R of validation trades; a different stop/target needs a retrain.
* Feedback for pattern heads is not accepted yet; they learn from the sidecar's labels only.
* The posterior table belongs to one model version: it restarts from the new holdout at each train.
* Training jobs live in memory; a restart forgets job status (the trained model is on disk).
