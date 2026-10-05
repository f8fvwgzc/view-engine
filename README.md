# View Engine

A local research engine. Create a project, ask a question, and a single **orchestrator** hires a team of
specialist agents for it, wires them into a dependency graph, lets them research the open web and exchange
findings, and returns a **decision-ready strategy** with sources. You watch the team being born, talking and
leaving in real time.

No credits, no API billing: agents run on **your own CLI subscriptions** (Claude Code, Codex, GitHub Copilot)
or on local models (Ollama, any OpenAI-compatible server). Efficiency features — prompt caching, compression,
memory — exist so those subscription limits go further.

## Run it

Requirements: Docker, Rust, Node 22+, and at least one provider (e.g. `claude` logged in).

```bash
make dev
```

This starts Postgres (pgvector, host port 5433) and Redis in Docker, then the API on `http://localhost:3001`,
the UI on `http://localhost:5173` and the quant sidecar on `http://127.0.0.1:8090` on your machine (needs `uv`). The API runs on the host so it can launch your CLIs.
URLs are shareable: `?project=…&task=…&agent=…`.

Other targets: `make test`, `make containers` (everything in Docker; Ollama/OpenAI-compatible/simulated only).

## How a run works

1. **Orchestrator** (the only agent at birth) recalls project memory, reads the skill catalog and hires a team:
   intent analyst → parallel domain researchers → analysts → strategist → critic. Each agent gets a role,
   an objective, 0–3 SKILL.md skills, a manager (`reports_to`) and its dependencies.
2. **Graph execution**: agents start when their dependencies finish (concurrency-limited), receive compact
   reports from upstream agents, browse the web, and iterate (`continue`/`done`) within their iteration limit.
   Agents may request extra specialists mid-run; the orchestrator validates and wires them in.
3. **Critic loop**: the critic can send the team back for a bounded follow-up round with new specialists and a
   revised strategist.
4. **Decision**: recommendation, confidence, scored options, flip conditions, next actions, full report, sources.
5. **Memory**: findings and the decision are stored, deduplicated, and consolidated into long-term observations
   that the next run in the same project recalls.

## Trading desk mode

Create a task with **Trading desk** selected, name an instrument (USDJPY, EURUSD, XAUUSD, DXY, SPY…) or let the
desk detect it, pick a horizon and optional timeframes, and drop/paste chart screenshots.

1. The quant sidecar (`quant/`, `make quant`, port 8090) builds a **market data pack** from free sources: OHLC for
   every timeframe (Yahoo/Stooq), indicators and support/resistance zones, cross-asset correlations (DXY, yields,
   JPY, gold, equities), the ForexFactory economic calendar, trading sessions, options positioning from ETF option
   chains (OI walls, put/call, max pain, approximate dealer gamma — FXY/GLD/UUP/SPY proxies, ~15 min delayed) and
   a LightGBM model's P(up) + expected range with walk-forward validation.
2. The orchestrator hires a desk: chart reader (reads your screenshots), macro & calendar, news impact,
   correlation, options positioning, quant/ML, risk manager, head trader, critic.
3. The head trader returns a **trade plan** (direction, calibrated probability, entry zone, stop, targets, timing,
   key events, scenarios, invalidation).
4. Every plan is recorded and **scored automatically** after its horizon (target/stop/direction, Brier score);
   results show under CALLS and feed long-term memory, so the desk learns what worked.

Outputs are probabilities with a measured track record, not guarantees or financial advice. Realtime CME/OTC FX
options and tick data need a paid feed; the sidecar's data layer is where such adapters plug in.

## RLCD: the calibrated decision model (`backend/rlcd`)

An independent API (port 8095, `make rlcd`) that answers typed questions with probabilities instead of text. It
follows the System One contract (one request = a `state` plus typed `questions`; answers are a **choice**, a
**score** or a **noul**, the probability that a statement is true), and it is our own outcome-trained model, not a
language model: every question maps to a trained head.

- **Intent heads** decide what a "Just ask" task wants: a position or research, day trade or swing. Below a
  confidence gate the keyword rules decide instead, and the event log says which did.
- **Setup heads** (`setup:5m`, `setup:15m`, `setup:1h`) answer which of buy, sell or neither reaches its target
  before its stop from the latest candle close, with the fixed day-trade risk (stop 20 pips, targets 50 and 100).
- **Pattern nouls** answer yes/no questions only when the pattern is on the chart: continuation after a pullback,
  fakeout, retest then continue.
- **Training data** comes from the quant sidecar: years of one-minute history stored locally (`quant/.cache/m1`,
  imported from free sources or your own MetaTrader exports in `quant/data/import/`), candles built from it, and for
  every candle the chart-structure, session, news-timing and currency-strength features with what happened next.
- **Calibration** uses sklearn's `CalibratedClassifierCV` on time-ordered folds; the most recent 20% of time is
  never trained on and every number shown in Settings → RLCD MODEL comes from it.
- **Bernoulli layer**: each decision is a win/lose trial. The model reports expected R, the Kelly fraction and a
  running win-rate estimate per probability band, and may only say "act" when that estimate beats breakeven with
  90% probability. Until the held-back trades beat breakeven, every decision is `hold`, with the reason.

`make rlcd-train` retrains everything. `POST /v1/systemone`, `/v1/rank`, `/v1/decide`, `/v1/scan`, `/v1/feedback`,
`/v1/train`, `GET /v1/calibration`, `/v1/bernoulli` are documented in `backend/rlcd/README.md`.

## Engine pieces (`backend/src/harness`)

| Module | What it does |
|---|---|
| `orchestrator.rs` | Hiring, DAG validation, scheduler with retry/backoff, agent loop, critic rounds, final decision |
| `router.rs` | Model router: role → `provider:model`, ordered fallback chain on usage limits / unavailability |
| `providers.rs` | Adapters: Claude Code CLI (integrated, lean `claude -p` mode), Codex, Copilot, Ollama, OpenAI-compatible, simulated |
| `skills.rs` + `skills/` (yours) | User-owned SKILL.md library: in-memory index (inverted-index BM25 + vector similarity, RRF-fused) shortlists skills for the orchestrator and resolves them per agent without a model call; outcome stats nudge ranking; GitHub import |
| `memory.rs` | Always-on long-term memory: pgvector + full-text hybrid recall fused by RRF, dedup, consolidation into observations |
| `compress.rs` | HTML→text, dense-line elision, BM25 extractive compression, `never_worse` |
| `ledger.rs` | Token accounting (input/output/cache read/write, tokens saved) and content-addressed response cache |
| `web.rs` | Search/fetch for providers without native browsing (DuckDuckGo, Wikipedia, optional SearXNG), cached |

Claude Code calls run with `--strict-mcp-config --setting-sources "" --system-prompt-file …` and only the
WebSearch/WebFetch tools, which cut the fixed context per call from ~33k to a few hundred tokens. The shared
system prompt is byte-identical across agents so the provider prompt cache is reused within a run.

Codex, Copilot, Ollama and OpenAI-compatible adapters are implemented but not yet exercised end to end.

## Skills

View Engine ships **without** skills. Your library lives in `skills/` (git-ignored): write skills in
Settings → Skills, drop `skills/<name>/SKILL.md` folders in, or import packs from public GitHub repos
(e.g. `anthropics/skills`). See [skills/README.md](skills/README.md). Agents run fine with an empty library.

## Proof, not just predictions

- **Backtest** (CALLS → watchlist → interval button): the body-close structure rules replayed over history with
  costs — win rate vs breakeven with a confidence interval, expectancy in R, breakdowns by session and signal.
  A verdict of *edge* needs the interval to clear breakeven, positive expectancy and ≥100 trades.
- **Scored calls**: every trade plan is checked against the market after its horizon (hit rate, Brier).
- **Skill stats**: each skill's uses and outcomes (critic verdicts, scored calls) feed its ranking.

## Configuration

Settings (UI → gear icon) hold run defaults, the model router, the skill library and appearance (fonts, text
size, accent colour, header and panel sizes — stored per browser). Backend env vars: `CLAUDE_BIN`, `CLAUDE_MODEL`, `CODEX_BIN`,
`CODEX_MODEL`, `COPILOT_BIN`, `OLLAMA_URL`, `OLLAMA_MODEL`, `OPENAI_BASE_URL`, `OPENAI_MODEL`,
`OPENAI_API_KEY`, `SEARXNG_URL`, `SKILLS_DIR`, `VIEW_ENGINE_WORKDIR`, `BIND_ADDR` (default `127.0.0.1`; the API has no
authentication, keep it local). With Ollama's `nomic-embed-text` pulled,
memory uses semantic embeddings; otherwise a local hashing embedder is used.

`POST /api/events` accepts external events (scripts, hooks) into the live stream.

Attributions for ported open-source patterns are in [NOTICE](NOTICE). Add authentication before exposing this
beyond your machine.
