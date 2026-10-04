-- Research harness: the fixed five-agent coding swarm is replaced by agents that are born per run.
DROP TABLE IF EXISTS agents;

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS config JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS latest_run_id UUID;

-- One execution of a task. A task can be re-run; every run keeps its own graph, ledger and report.
CREATE TABLE IF NOT EXISTS runs (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'planning',
    round INT NOT NULL DEFAULT 1,
    provider TEXT NOT NULL,
    model TEXT,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    intent JSONB,
    report TEXT,
    decision JSONB,
    error TEXT,
    tokens_in BIGINT NOT NULL DEFAULT 0,
    tokens_out BIGINT NOT NULL DEFAULT 0,
    tokens_cache_read BIGINT NOT NULL DEFAULT 0,
    tokens_cache_write BIGINT NOT NULL DEFAULT 0,
    tokens_saved BIGINT NOT NULL DEFAULT 0,
    cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    llm_calls INT NOT NULL DEFAULT 0,
    cache_hits INT NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS runs_task_idx ON runs(task_id, started_at DESC);

-- Agents born for a run, wired into a dependency graph through depends_on (keys of other agents in the same run).
CREATE TABLE IF NOT EXISTS run_agents (
    id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    kind TEXT NOT NULL,
    objective TEXT NOT NULL,
    -- Org chart (who hired / supervises this agent) is separate from the data-flow graph (depends_on).
    reports_to TEXT,
    hired_by TEXT,
    skills TEXT[] NOT NULL DEFAULT '{}',
    depends_on TEXT[] NOT NULL DEFAULT '{}',
    round INT NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt INT NOT NULL DEFAULT 0,
    provider TEXT NOT NULL,
    model TEXT,
    budget_usd DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    max_iterations INT NOT NULL DEFAULT 3,
    iterations INT NOT NULL DEFAULT 0,
    output TEXT,
    summary TEXT,
    sources JSONB NOT NULL DEFAULT '[]'::jsonb,
    tokens_in BIGINT NOT NULL DEFAULT 0,
    tokens_out BIGINT NOT NULL DEFAULT 0,
    tokens_cache_read BIGINT NOT NULL DEFAULT 0,
    tokens_cache_write BIGINT NOT NULL DEFAULT 0,
    cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    error TEXT,
    next_retry_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, key)
);
CREATE INDEX IF NOT EXISTS run_agents_run_idx ON run_agents(run_id);

ALTER TABLE swarm_events ADD COLUMN IF NOT EXISTS run_id UUID REFERENCES runs(id) ON DELETE CASCADE;
ALTER TABLE swarm_events ADD COLUMN IF NOT EXISTS data JSONB;
ALTER TABLE swarm_events DROP COLUMN IF EXISTS embedding;
CREATE INDEX IF NOT EXISTS swarm_events_run_idx ON swarm_events(run_id, created_at DESC);

-- Long-term project memory (patterns from vectorize-io/hindsight, MIT, and mem0ai/mem0, Apache-2.0).
-- Hybrid retrieval: weighted full-text (lexical arm) + pgvector cosine (semantic arm), fused with RRF k=60.
-- kind: world (external fact) | experience (what an agent did) | opinion (judgement) | observation (consolidated) | decision.
CREATE TABLE IF NOT EXISTS memories (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id UUID REFERENCES tasks(id) ON DELETE SET NULL,
    run_id UUID REFERENCES runs(id) ON DELETE SET NULL,
    agent_key TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('world', 'experience', 'opinion', 'observation', 'decision')),
    content TEXT NOT NULL,
    context TEXT,
    -- entity names and spelled-out dates, fed to the lexical arm (Hindsight `text_signals`)
    signals TEXT,
    entities TEXT[] NOT NULL DEFAULT '{}',
    source_url TEXT,
    importance REAL NOT NULL DEFAULT 0.5 CHECK (importance BETWEEN 0 AND 1),
    confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    proof_count INT NOT NULL DEFAULT 1,
    source_memory_ids UUID[] NOT NULL DEFAULT '{}',
    superseded_by UUID REFERENCES memories(id) ON DELETE SET NULL,
    consolidated_at TIMESTAMPTZ,
    content_hash TEXT GENERATED ALWAYS AS (md5(lower(regexp_replace(btrim(content), '\s+', ' ', 'g')))) STORED,
    search_vector tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', content), 'A') ||
        setweight(to_tsvector('simple', coalesce(signals, '')), 'B') ||
        setweight(to_tsvector('english', coalesce(context, '')), 'C')) STORED,
    embedding vector(768),
    embed_model TEXT NOT NULL,
    occurred_at TIMESTAMPTZ,
    access_count INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_accessed_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS memories_live_hash_idx ON memories (project_id, content_hash) WHERE superseded_by IS NULL;
CREATE INDEX IF NOT EXISTS memories_search_idx ON memories USING GIN (search_vector);
CREATE INDEX IF NOT EXISTS memories_project_kind_idx ON memories (project_id, kind, created_at DESC);
CREATE INDEX IF NOT EXISTS memories_unconsolidated_idx ON memories (project_id) WHERE consolidated_at IS NULL AND kind <> 'observation';
-- Vectors from different embedding models are not comparable: one partial HNSW index per model.
CREATE INDEX IF NOT EXISTS memories_hnsw_hash_idx ON memories USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64) WHERE embed_model = 'hash-v1';
CREATE INDEX IF NOT EXISTS memories_hnsw_nomic_idx ON memories USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64) WHERE embed_model = 'nomic-embed-text';

CREATE TABLE IF NOT EXISTS memory_history (
    id BIGSERIAL PRIMARY KEY,
    memory_id UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    event TEXT NOT NULL,
    old_content TEXT,
    new_content TEXT,
    reason TEXT,
    at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Content-addressed caches: identical prompts and identical web fetches are never paid for twice.
CREATE TABLE IF NOT EXISTS llm_cache (
    key TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT,
    response TEXT NOT NULL,
    usage JSONB NOT NULL DEFAULT '{}'::jsonb,
    hits INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS web_cache (
    key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS settings (
    id INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    value JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO settings (id, value) VALUES (1, '{}'::jsonb) ON CONFLICT (id) DO NOTHING;
