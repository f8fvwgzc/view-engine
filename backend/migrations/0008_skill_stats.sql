-- Outcome statistics per skill: how often it was attached to an agent and how the runs it took part in turned out
-- (critic verdicts for research, scored predictions for trading). Used as a small ranking prior in skill selection.
CREATE TABLE IF NOT EXISTS skill_stats (
    skill TEXT PRIMARY KEY,
    uses INT NOT NULL DEFAULT 0,
    accepted INT NOT NULL DEFAULT 0,
    revised INT NOT NULL DEFAULT 0,
    hits INT NOT NULL DEFAULT 0,
    misses INT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Long-term memory is always on; drop the old toggle from saved settings.
UPDATE settings SET value = value - 'memory' WHERE value ? 'memory';
