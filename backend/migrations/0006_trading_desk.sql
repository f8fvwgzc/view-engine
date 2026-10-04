-- Trading desk mode: chart screenshots attached to tasks, the market data pack used by a run, and a prediction
-- ledger that scores every trade plan once its horizon has passed (hit rate, Brier score), feeding memory.
CREATE TABLE IF NOT EXISTS attachments (
    id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    path TEXT NOT NULL,
    mime TEXT NOT NULL,
    bytes BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS attachments_task_idx ON attachments(task_id);

ALTER TABLE runs ADD COLUMN IF NOT EXISTS market JSONB;

CREATE TABLE IF NOT EXISTS predictions (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('long', 'short', 'neutral')),
    probability REAL NOT NULL CHECK (probability BETWEEN 0 AND 1),
    horizon_hours INT NOT NULL,
    reference_price DOUBLE PRECISION NOT NULL,
    entry_low DOUBLE PRECISION,
    entry_high DOUBLE PRECISION,
    stop DOUBLE PRECISION,
    targets DOUBLE PRECISION[] NOT NULL DEFAULT '{}',
    plan JSONB NOT NULL,
    model_prob_up REAL,
    evaluate_at TIMESTAMPTZ NOT NULL,
    evaluated_at TIMESTAMPTZ,
    outcome TEXT CHECK (outcome IN ('target', 'stop', 'correct', 'wrong', 'flat', 'unscored')),
    outcome_price DOUBLE PRECISION,
    return_pct DOUBLE PRECISION,
    brier DOUBLE PRECISION,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS predictions_due_idx ON predictions(evaluate_at) WHERE evaluated_at IS NULL;
CREATE INDEX IF NOT EXISTS predictions_project_idx ON predictions(project_id, created_at DESC);
