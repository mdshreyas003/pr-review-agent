-- ============================================================================
-- Manual, on-demand code-memory indexing. One row per triggered run - never
-- created by a scheduler, poller, or webhook, only by an explicit human action
-- through POST /api/index/{repository_id}.
-- ============================================================================

CREATE TABLE IF NOT EXISTS index_runs (
    run_id            UUID        PRIMARY KEY,
    repository_id     TEXT        NOT NULL,
    project           TEXT        NOT NULL DEFAULT '',
    repository_name   TEXT        NOT NULL DEFAULT '',
    branch            TEXT        NOT NULL DEFAULT '',
    commit_sha        TEXT        NOT NULL DEFAULT '',
    replace_existing  BOOLEAN     NOT NULL DEFAULT false,
    embed_requested   BOOLEAN     NOT NULL DEFAULT true,
    -- Distinct from embed_requested: true only once embeddings actually ran,
    -- so a request that silently degraded to keyword-only stays visible.
    embed_effective   BOOLEAN     NOT NULL DEFAULT false,
    status            TEXT        NOT NULL DEFAULT 'queued', -- queued|running|completed|failed
    files_collected   INTEGER     NOT NULL DEFAULT 0,
    written           INTEGER     NOT NULL DEFAULT 0,
    unchanged         INTEGER     NOT NULL DEFAULT 0,
    superseded        INTEGER     NOT NULL DEFAULT 0,
    removed_files     INTEGER     NOT NULL DEFAULT 0,
    error             TEXT,
    triggered_by      TEXT        NOT NULL DEFAULT '',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at        TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS index_runs_repo_idx ON index_runs (repository_id, created_at DESC);
