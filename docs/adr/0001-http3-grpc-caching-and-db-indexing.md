# ADR 0001 — HTTP/3 at the edge, gRPC between services, two-tier caching, and a database index/key/trigger pass

- **Status:** Accepted
- **Date:** 2026-10-24
- **Scope:** `edge/`, `proto/`, `backend/src/{sidecar,cache,http_cache}.rs`, `backend/src/harness/{market,rlcd}.rs`,
  `quant/app/main.py`, `backend/rlcd/rlcd/api.py`, `backend/migrations/0009_indexes_keys_triggers.sql`,
  `frontend/src/api.ts`, `docker-compose.yml`, `Makefile`

## Context

View Engine is one Rust API (axum) in front of two Python sidecars (quant: market data and analytics; RLCD: the
calibrated decision model), a React UI, Postgres (pgvector) and Redis.

Before this change:

- **Browser ⇄ API:** plain HTTP/1.1 through the Vite dev proxy. API answers had no `Cache-Control` or validators,
  so every poll of the dashboard and every chart re-downloaded the full JSON.
- **API ⇄ sidecars:** a fresh HTTP/1.1 JSON request per call (`reqwest`). Nothing was cached on the API side, so ten
  agents of one run asking for the same snapshot made ten identical sidecar computations. The quant sidecar had a
  small in-process TTL cache; RLCD had none.
- **Postgres:** nine foreign keys had no index on the referencing column (every parent `DELETE` seq-scanned the
  child under lock), the dashboard's "newest 200 events" and several list queries had no matching index, two
  indexes were redundant, `tasks.latest_run_id` was an undeclared foreign key, and `updated_at` / `completed_at`
  depended on every writer remembering to set them.

## Decision

### 1. HTTP/3 between the browser and the services: a Caddy edge

```
browser ──HTTP/3 (QUIC/UDP) · HTTP/2 · HTTP/1.1──▶ Caddy edge :8443 ──h2c──▶ axum /api/*
                                                                     ──HTTP/1.1──▶ axum /ws (WebSocket)
                                                                     ──▶ /srv (built UI)  or  Vite :5173 (dev)
```

- **Caddy 2.10** terminates TLS and speaks HTTP/3, HTTP/2 and HTTP/1.1 on the same port (TCP and UDP published),
  advertising h3 with `Alt-Svc`. Browsers that cannot use QUIC fall back without any configuration.
- `https_port` equals the published port, so the `Alt-Svc: h3=":8443"` advertisement points where the browser can
  actually reach it (a `8443→443` mapping would advertise a port that is not published).
- **Edge → API over h2c** (HTTP/2 cleartext, axum `http2` feature): one multiplexed connection instead of a pool.
  `/ws` stays on HTTP/1.1 because axum 0.7 upgrades WebSockets only there.
- **Dev:** `edge/Caddyfile.dev` (service `edge`, started by `make infra`) proxies `/api` and `/ws` to the API on the
  host and everything else to Vite, HMR included. **Containers:** `frontend/Dockerfile` builds the UI and bakes it
  into a Caddy image with `edge/Caddyfile`.
- Compression (`zstd`, `gzip`) is done once, at the edge.
- Why not HTTP/3 in axum itself: the Rust HTTP/3 stack (`h3`/`quinn`) is still experimental for servers, would not
  cover the static UI, and an edge is where TLS, compression and certificates belong anyway.

**Certificates and browsers.** For `localhost` Caddy issues a certificate from its own local CA. `make edge-trust`
adds that CA to the macOS keychain. Browsers restrict HTTP/3 with locally installed roots: Chrome needs
`--origin-to-force-quic-on=localhost:8443`, Firefox needs
`network.http.http3.disable_when_third_party_roots_found=false`. Without them they use HTTP/2 on the same port,
which is still correct. With a public `SITE_ADDRESS` (and `EDGE_PORT=443`, port 80 published) Caddy gets a Let's
Encrypt certificate and HTTP/3 works in every browser with no flags.

### 2. HTTP caching between the browser and the API

`backend/src/http_cache.rs` is a middleware on every `GET`/`HEAD` under `/api/`:

| Route | `Cache-Control` | Why |
|---|---|---|
| `/api/market/*` with `as_of` ≥ 1 h in the past | `private, max-age=86400, immutable` | replays use closed candles only |
| other `/api/market/*` | `private, max-age=15, stale-while-revalidate=45` | 15-minute bars; instant re-render, background refresh |
| `/api/market/symbols` | `private, max-age=3600, stale-while-revalidate=86400` | changes with releases |
| `/api/rlcd/{heads,calibration,bernoulli}` | `private, max-age=60, stale-while-revalidate=300` | change only after training |
| health, `/api/rlcd/train*` | `no-store` | live status |
| everything else (dashboard, runs, tasks, settings…) | `no-cache` | always current; revalidated with the ETag |
| `/api/attachments/:id` (set by the handler) | `private, max-age=31536000, immutable` | an id always names the same bytes |

JSON answers get a weak ETag (`W/"<sha256/96>"`); a matching `If-None-Match` gets a **304 with no body**, so the
UI's frequent `no-cache` polls cost headers only when nothing changed. The browser HTTP cache does the rest; the
UI only adds de-duplication of identical in-flight GETs (`frontend/src/api.ts`) and stops sending
`Content-Type` on bodiless GETs (no CORS preflight when the API is on another origin). Static assets are
fingerprinted by Vite and served `immutable` by the edge; `index.html` is `no-cache` so a deploy shows up on the
next load.

### 3. gRPC between services

`proto/viewengine/sidecar/v1/sidecar.proto` defines one RPC:

```proto
service Sidecar { rpc Call(CallRequest) returns (CallResponse); }
// CallRequest: method, path, query, body (JSON), if_none_match
// CallResponse: status, body (JSON), content_type, cache_control, etag
```

- **Server (Python):** `proto/python/view_engine_grpc` is a small shared package (path dependency of both
  sidecars). Its servicer replays each call into the sidecar's own FastAPI app in-process over ASGI (no socket),
  so routes, validation, error handlers, auth (RLCD's API key travels as `authorization` metadata) and cache policy
  are written once and served identically on HTTP (8090 / 8095) and gRPC (9090 / 9095). The `.proto` is loaded at
  runtime with `grpc.protos_and_services`: there are no generated Python files to keep in sync. `grpc.aio` runs on
  the app's event loop, started and stopped by its lifespan; gzip responses, 64 MB message limit, keepalive pings
  accepted.
- **Client (Rust):** `backend/src/sidecar.rs`, generated by `tonic-build` from the same `.proto` at build time
  (`protoc` is vendored). One lazily connected HTTP/2 channel per sidecar with TCP_NODELAY, keepalive every 30 s,
  adaptive flow control, gzip accepted; the call timeout becomes the gRPC deadline. Bodies are `bytes::Bytes` from
  the wire to the cache to Redis without copies.
- **Fallback:** if the gRPC port refuses (`Unavailable`/`Unimplemented`: sidecar not yet updated, bridge disabled
  with `QUANT_GRPC_PORT=0`), the call goes to the HTTP route and gRPC is skipped for 30 s. Deadline and other errors
  are returned as is (no retry that would double the latency).
- **Why one envelope RPC instead of a typed RPC per route:** the sidecars have ~40 routes returning large, evolving
  JSON documents (snapshots, charts, level maps) that the API passes through or reads loosely. A typed proto per
  route would duplicate every response schema in three languages and turn each analytics change into a contract
  change, for no gain on these payloads. The envelope still gives what gRPC is for here: persistent multiplexed
  HTTP/2, deadlines, compression, keepalive and a typed cache/validation channel. A route that becomes hot and
  stable can get its own typed RPC next to `Call` without touching the others.
- **RLCD → quant** stays on HTTP: it streams multi-megabyte training datasets (zip), which is what HTTP does well
  and unary gRPC does not.
- Config: `QUANT_GRPC_URL` (default `http://127.0.0.1:9090`), `RLCD_GRPC_URL` (`…:9095`), `off` for HTTP only;
  servers bind `SIDECAR_GRPC_BIND` (default `127.0.0.1`).

### 4. Service-to-service cache

`backend/src/cache.rs`, used by `Sidecar::call` for idempotent calls (all GETs, and RLCD's read-only POSTs
`/v1/systemone`, `/v1/decide`, `/v1/scan`, `/v1/rank`):

- **L1, in process (moka):** TinyLFU admission, 128 MB weighted by bytes, per-entry expiry. Concurrent identical
  calls are **coalesced** into one sidecar call (`try_get_with`).
- **L2, Redis:** shared by every API process and survives restarts; entries > 8 MB stay in L1 only; every Redis
  operation has a 250 ms budget, and with Redis down the cache runs on L1 alone.
- **Freshness is declared by the sidecar**, next to its routes (`CACHE_RULES` in `quant/app/main.py` and
  `backend/rlcd/rlcd/api.py`, applied by `view_engine_grpc.policy`) as `Cache-Control` on 200 answers. A route with
  no rule is `no-store`. Quant TTLs stay at or below its own in-process TTLs; replays are immutable for a day.
- **Stale-while-revalidate:** inside max-age an entry is served as is; inside the stale window it is served and
  one background call per key refreshes it, sending the cached ETag — when nothing changed the sidecar answers
  **304 without a body** over gRPC, so a refresh costs the computation but not the payload.
- **Invalidation:** keys carry a per-namespace generation (`viewengine:cache:gen:{quant|rlcd}` in Redis). Starting
  a training run and seeing a training job finish (`succeeded`/`partial`) bump RLCD's generation, dropping every
  cached answer of the old model at once.
- Errors are never cached; non-idempotent calls (training, feedback) always reach the sidecar.
  `SIDECAR_CACHE=off` disables the whole cache.

### 5. Database: indexes, keys, triggers (`0009_indexes_keys_triggers.sql`)

- **Every foreign key indexed** on its referencing column: `swarm_events.task_id`, `runs.project_id`,
  `run_agents.task_id`, `memories.{task_id, run_id, superseded_by}`, `memory_history.memory_id`,
  `predictions.{task_id, run_id}`, `tasks.latest_run_id`. Mostly-null columns get partial (`IS NOT NULL`) indexes;
  the FK check `col = $1` still uses them.
- **Missing FK declared:** `tasks.latest_run_id → runs(id) ON DELETE SET NULL` (dangling ids cleared first).
- **Query-shaped indexes:** newest events (`swarm_events(created_at DESC)`), task / project / prediction lists by
  `created_at DESC`, a run's agents `(run_id, created_at)`, a task's attachments `(task_id, created_at)`, live
  memories `(project_id, created_at DESC) WHERE superseded_by IS NULL`, and the consolidation queue
  `(project_id, importance DESC, created_at DESC)` with the query's exact predicate (index range read, no sort).
  Cache tables get `created_at` for pruning by age.
- **Redundant indexes dropped:** `run_agents_run_idx` (covered by `UNIQUE (run_id, key)`),
  `attachments_task_idx` and `memories_unconsolidated_idx` (replaced by the wider ones above). Tiny tables
  (`watchlist`, `skill_stats`) get nothing extra: a sequential scan is the best plan there.
- **Triggers:** `touch_updated_at()` on `memories` (only when content, importance, confidence, proofs, sources or
  supersession change; reads do not count), `settings` and `skill_stats`; `stamp_completed_at()` on `runs` and
  `run_agents` for final statuses reached without a timestamp (the cancel and restart bulk updates).
- **Storage:** `fillfactor` 85 on `runs` / `run_agents` (token counters and status update many times per run; none
  of those columns is indexed, so with free page space the updates are HOT), 70 on `watchlist`, 90 on `memories`
  / `llm_cache`; insert-driven autovacuum and earlier analyze on the append-only `swarm_events` and on `memories`.
- **Server settings** (`docker-compose.yml`): SSD planner costs (`random_page_cost=1.1`,
  `effective_io_concurrency=200`), sized `shared_buffers` / `work_mem`, `jit=off` for short OLTP queries,
  `track_io_timing`, and `pg_stat_statements` (created by the migration when preloaded, skipped with a notice
  otherwise). The API pool keeps 2 warm connections, waits at most 5 s for one and recycles idle ones after 10 min.
- `CREATE INDEX CONCURRENTLY` is not used: sqlx runs each migration in a transaction, and these tables are small.
  A large deployment should build such indexes by hand with `CONCURRENTLY` before deploying the migration (the
  `IF NOT EXISTS` clauses make it a no-op then).

## Consequences

- One HTTPS origin (`https://localhost:8443`) for UI, API and live stream; HTTP/3 where the browser allows it.
- Repeat reads are browser cache hits or 304s; identical sidecar work within a TTL is done once per cluster, and
  concurrent identical calls once per process.
- Old sidecars (no bridge) and HTTP-only setups keep working through the fallback.
- New moving parts: Caddy, the shared Python package, tonic/prost/moka in the API. The `.proto` is the single
  contract for both languages.
- Data can be up to one TTL old (15–60 s for market reads). Every TTL is written next to its route and can be
  lowered there.

## Rollout and verification

This change was written without building or running anything. Before relying on it:

```bash
cd quant && uv lock && uv sync          # adds view-engine-grpc (grpcio, grpcio-tools) to the lock
cd backend/rlcd && uv lock && uv sync
make test                                # cargo test also runs the new cache / http_cache unit tests
make dev                                 # then open https://localhost:8443
```

Checks:

- `curl -sk --http3 -I https://localhost:8443/api/health` (curl built with HTTP/3) → `HTTP/3 200`; a normal request
  shows `alt-svc: h3=":8443"`.
- `curl -sk -I https://localhost:8443/api/dashboard` → `etag` and `cache-control: no-cache`; repeat it with
  `-H 'If-None-Match: <etag>'` → `304`.
- Quant log: `gRPC bridge listening on 127.0.0.1:9090`; API with `RUST_LOG=view_engine_api=debug` shows no
  "falling back to HTTP" lines.
- `redis-cli --scan --pattern 'viewengine:cache:*'` lists cached answers after opening a chart.
- `SELECT indexrelname, idx_scan FROM pg_stat_user_indexes ORDER BY idx_scan;` and `pg_stat_statements` to watch the
  new indexes being used.
