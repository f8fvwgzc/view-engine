-- Index, key and trigger pass over the whole schema (docs/adr/0001-http3-grpc-caching-and-db-indexing.md).
--
-- sqlx runs a migration in one transaction, so CREATE INDEX CONCURRENTLY is not available here; the tables of a
-- local research tool are small enough for a plain build. IF [NOT] EXISTS everywhere: safe to re-run by hand.

-- ---------------------------------------------------------------- 1. every foreign key gets an index
-- Postgres indexes the referenced (primary) side only. Without an index on the referencing columns, every DELETE
-- of a parent (project, task, run, memory) seq-scans each child table to cascade or set null, holding locks.
-- Partial (IS NOT NULL) where the column is mostly null: the FK check `col = $1` still proves the predicate.
CREATE INDEX IF NOT EXISTS swarm_events_task_idx ON swarm_events (task_id) WHERE task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS runs_project_idx ON runs (project_id, started_at DESC);
CREATE INDEX IF NOT EXISTS run_agents_task_idx ON run_agents (task_id);
CREATE INDEX IF NOT EXISTS memories_task_idx ON memories (task_id) WHERE task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS memories_run_idx ON memories (run_id) WHERE run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS memories_superseded_by_idx ON memories (superseded_by) WHERE superseded_by IS NOT NULL;
CREATE INDEX IF NOT EXISTS memory_history_memory_idx ON memory_history (memory_id, at DESC);
CREATE INDEX IF NOT EXISTS predictions_task_idx ON predictions (task_id);
CREATE INDEX IF NOT EXISTS predictions_run_idx ON predictions (run_id);
-- Already covered by an index that leads with the FK column: tasks.project_id, swarm_events.project_id and run_id,
-- runs.task_id, run_agents.run_id (UNIQUE (run_id, key)), memories.project_id, attachments.task_id,
-- watchlist.project_id (UNIQUE (project_id, symbol)).

-- ---------------------------------------------------------------- 2. the missing foreign key
-- tasks.latest_run_id points at a run but was never declared: a deleted run left a dangling id behind.
UPDATE tasks t SET latest_run_id = NULL
WHERE latest_run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.id = t.latest_run_id);
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tasks_latest_run_id_fkey') THEN
        ALTER TABLE tasks ADD CONSTRAINT tasks_latest_run_id_fkey
            FOREIGN KEY (latest_run_id) REFERENCES runs (id) ON DELETE SET NULL;
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS tasks_latest_run_idx ON tasks (latest_run_id) WHERE latest_run_id IS NOT NULL;

-- ---------------------------------------------------------------- 3. indexes shaped like the queries
-- Dashboard: newest 200 events across all projects; the task list and the project list, newest first.
CREATE INDEX IF NOT EXISTS swarm_events_created_idx ON swarm_events (created_at DESC);
CREATE INDEX IF NOT EXISTS tasks_created_idx ON tasks (created_at DESC);
CREATE INDEX IF NOT EXISTS projects_created_idx ON projects (created_at DESC);
-- Prediction book without a project filter (`$1 IS NULL OR project_id = $1 ORDER BY created_at DESC`).
CREATE INDEX IF NOT EXISTS predictions_created_idx ON predictions (created_at DESC);

-- A run's agents in hiring order. UNIQUE (run_id, key) already serves lookups by run, so the plain run_id index
-- was redundant write cost; this one also returns the rows sorted.
CREATE INDEX IF NOT EXISTS run_agents_run_created_idx ON run_agents (run_id, created_at);
DROP INDEX IF EXISTS run_agents_run_idx;

-- A task's attachments in upload order (same column first: replaces the plain task_id index).
CREATE INDEX IF NOT EXISTS attachments_task_created_idx ON attachments (task_id, created_at);
DROP INDEX IF EXISTS attachments_task_idx;

-- Live memories of a project, newest first (memory panel, prompt context, per-project counts).
CREATE INDEX IF NOT EXISTS memories_live_idx ON memories (project_id, created_at DESC) WHERE superseded_by IS NULL;
-- Consolidation queue: same predicate and sort as the query (`... AND superseded_by IS NULL ORDER BY importance
-- DESC, created_at DESC LIMIT 24`), so it is an index range read with no sort. Replaces memories_unconsolidated_idx.
CREATE INDEX IF NOT EXISTS memories_consolidation_queue_idx ON memories (project_id, importance DESC, created_at DESC)
    WHERE consolidated_at IS NULL AND kind <> 'observation' AND superseded_by IS NULL;
DROP INDEX IF EXISTS memories_unconsolidated_idx;

-- Content-addressed caches are read by primary key; this one makes pruning old entries by age cheap.
CREATE INDEX IF NOT EXISTS llm_cache_created_idx ON llm_cache (created_at);
CREATE INDEX IF NOT EXISTS web_cache_created_idx ON web_cache (created_at);
-- watchlist and skill_stats stay without extra indexes: a handful of rows, a sequential scan is the best plan.

-- ---------------------------------------------------------------- 4. triggers
-- updated_at maintained by the database, whoever writes the row.
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END $$;

-- Memories: only a change of what the memory says counts; reads (access_count, last_accessed_at) and the
-- consolidation stamp do not.
DROP TRIGGER IF EXISTS memories_touch ON memories;
CREATE TRIGGER memories_touch BEFORE UPDATE ON memories FOR EACH ROW
    WHEN (OLD.content IS DISTINCT FROM NEW.content
          OR OLD.importance IS DISTINCT FROM NEW.importance
          OR OLD.confidence IS DISTINCT FROM NEW.confidence
          OR OLD.proof_count IS DISTINCT FROM NEW.proof_count
          OR OLD.source_memory_ids IS DISTINCT FROM NEW.source_memory_ids
          OR OLD.superseded_by IS DISTINCT FROM NEW.superseded_by)
    EXECUTE FUNCTION touch_updated_at();

DROP TRIGGER IF EXISTS settings_touch ON settings;
CREATE TRIGGER settings_touch BEFORE UPDATE ON settings FOR EACH ROW
    WHEN (OLD.value IS DISTINCT FROM NEW.value) EXECUTE FUNCTION touch_updated_at();

DROP TRIGGER IF EXISTS skill_stats_touch ON skill_stats;
CREATE TRIGGER skill_stats_touch BEFORE UPDATE ON skill_stats FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- completed_at stamped when a run or an agent reaches a final status without one: the agent update in the
-- orchestrator sets it for complete/failed/skipped, but the bulk paths (cancel, restart) do not.
CREATE OR REPLACE FUNCTION stamp_completed_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.completed_at IS NULL AND NEW.status = ANY (TG_ARGV) THEN
        NEW.completed_at := NOW();
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS runs_completed ON runs;
CREATE TRIGGER runs_completed BEFORE UPDATE OF status ON runs FOR EACH ROW
    WHEN (OLD.status IS DISTINCT FROM NEW.status)
    EXECUTE FUNCTION stamp_completed_at('complete', 'failed', 'interrupted', 'cancelled');

DROP TRIGGER IF EXISTS run_agents_completed ON run_agents;
CREATE TRIGGER run_agents_completed BEFORE UPDATE OF status ON run_agents FOR EACH ROW
    WHEN (OLD.status IS DISTINCT FROM NEW.status)
    EXECUTE FUNCTION stamp_completed_at('complete', 'failed', 'skipped');

-- ---------------------------------------------------------------- 5. storage and autovacuum
-- Token counters and status change many times per run. With free space on each page those updates stay HOT
-- (heap-only: no index entries written), since none of the updated columns is indexed.
ALTER TABLE runs SET (fillfactor = 85);
ALTER TABLE run_agents SET (fillfactor = 85);
ALTER TABLE watchlist SET (fillfactor = 70);
ALTER TABLE memories SET (fillfactor = 90);
ALTER TABLE llm_cache SET (fillfactor = 90);
-- The event log only grows: vacuum (visibility map, index-only scans) and analyze by inserted rows instead of
-- waiting for 20% of a large table to change.
ALTER TABLE swarm_events SET (autovacuum_vacuum_insert_scale_factor = 0.05, autovacuum_analyze_scale_factor = 0.02);
ALTER TABLE memories SET (autovacuum_vacuum_scale_factor = 0.05, autovacuum_analyze_scale_factor = 0.02);

-- ---------------------------------------------------------------- 6. observability
-- Query statistics, when the server preloads the library (docker-compose.yml does). Anywhere else (no preload,
-- no contrib, not a superuser) this is skipped with a notice instead of failing the migration.
DO $$
BEGIN
    IF current_setting('shared_preload_libraries') LIKE '%pg_stat_statements%' THEN
        CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
    END IF;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'pg_stat_statements not enabled: %', SQLERRM;
END $$;

ANALYZE;
