-- Realtime structure watcher: instruments a project watches; the backend polls the quant sidecar's /signals on every
-- candle close and pushes fired signals (BOS, box breakouts, failed breakouts, sweeps) into the live stream.
CREATE TABLE IF NOT EXISTS watchlist (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    intervals TEXT[] NOT NULL DEFAULT '{4h,1h}',
    active BOOLEAN NOT NULL DEFAULT TRUE,
    seen TEXT[] NOT NULL DEFAULT '{}',
    last_checked_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, symbol)
);
