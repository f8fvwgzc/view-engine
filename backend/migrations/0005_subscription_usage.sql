-- CLI providers run on the user's own subscription limits and local models are free, so there is no money
-- budget: per-run/per-agent dollar columns are removed. Usage is tracked in tokens (incl. cache reads/writes).
ALTER TABLE runs DROP COLUMN IF EXISTS cost_usd;
ALTER TABLE run_agents DROP COLUMN IF EXISTS cost_usd;
ALTER TABLE run_agents DROP COLUMN IF EXISTS budget_usd;
-- Settings saved before the model router existed may contain a budget key; drop it.
UPDATE settings SET value = value - 'budget_usd' WHERE value ? 'budget_usd';
