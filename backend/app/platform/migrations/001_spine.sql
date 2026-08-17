-- ============================================================================
-- One database, three access shapes.
--
--   Lane 1  memory      code_chunks       vector + full-text, hybrid retrieval
--   Lane 2  truth       pr_review_records, finding_records, hitl_reviews
--   Lane 3  time        agent_events      append-only audit spine
--
-- pgvector is required. TimescaleDB is optional: blocks between the
-- {{TIMESCALE}} markers are stripped when the extension is unavailable, and
-- everything degrades to native partitioning.
--
-- No economics: no budget guard, no cost ledger, no per-agent cost rollups,
-- no finding-dispute feedback loop.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
-- {{TIMESCALE}}
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
-- {{/TIMESCALE}}


-- ---------------------------------------------------------------------------
-- Lane 1: memory
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS code_chunks (
    chunk_id        TEXT PRIMARY KEY,
    repository_id   TEXT        NOT NULL,
    project         TEXT        NOT NULL DEFAULT '',
    file_path       TEXT        NOT NULL,
    line_start      INTEGER     NOT NULL DEFAULT 0,
    line_end        INTEGER     NOT NULL DEFAULT 0,
    language        TEXT        NOT NULL DEFAULT '',
    symbol          TEXT        NOT NULL DEFAULT '',
    content         TEXT        NOT NULL,
    content_sha     TEXT        NOT NULL DEFAULT '',
    commit_sha      TEXT        NOT NULL DEFAULT '',
    embedding       vector(256),
    -- Generated, so the keyword lane can never drift from the content it indexes.
    content_tsv     tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    indexed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS code_chunks_repo_idx     ON code_chunks (repository_id);
CREATE INDEX IF NOT EXISTS code_chunks_path_idx     ON code_chunks (repository_id, file_path);
CREATE INDEX IF NOT EXISTS code_chunks_tsv_idx      ON code_chunks USING GIN (content_tsv);
CREATE INDEX IF NOT EXISTS code_chunks_symbol_idx   ON code_chunks USING GIN (symbol gin_trgm_ops);
-- Cosine distance, matching the query in memory/retrieval.py.
CREATE INDEX IF NOT EXISTS code_chunks_embedding_idx
    ON code_chunks USING hnsw (embedding vector_cosine_ops);


-- ---------------------------------------------------------------------------
-- Lane 2: truth
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pr_review_records (
    review_id          UUID PRIMARY KEY,
    idempotency_key    TEXT        NOT NULL UNIQUE,
    organization_url   TEXT        NOT NULL,
    project            TEXT        NOT NULL,
    repository_id      TEXT        NOT NULL,
    repository_name    TEXT        NOT NULL,
    pull_request_id    INTEGER     NOT NULL,
    title              TEXT        NOT NULL DEFAULT '',
    author             TEXT        NOT NULL DEFAULT '',
    source_branch      TEXT        NOT NULL DEFAULT '',
    target_branch      TEXT        NOT NULL DEFAULT '',
    source_commit      TEXT        NOT NULL DEFAULT '',
    event_type         TEXT        NOT NULL DEFAULT '',
    status             TEXT        NOT NULL DEFAULT 'queued',
    overall_confidence REAL        NOT NULL DEFAULT 0,
    requires_human     BOOLEAN     NOT NULL DEFAULT false,
    escalated          BOOLEAN     NOT NULL DEFAULT false,
    summary            TEXT        NOT NULL DEFAULT '',
    duration_ms        INTEGER     NOT NULL DEFAULT 0,
    error              TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS pr_review_status_idx  ON pr_review_records (status, created_at DESC);
CREATE INDEX IF NOT EXISTS pr_review_pr_idx      ON pr_review_records (repository_id, pull_request_id);
CREATE INDEX IF NOT EXISTS pr_review_created_idx ON pr_review_records (created_at DESC);

CREATE TABLE IF NOT EXISTS finding_records (
    finding_id   UUID PRIMARY KEY,
    review_id    UUID        NOT NULL REFERENCES pr_review_records(review_id) ON DELETE CASCADE,
    agent_type   TEXT        NOT NULL,
    severity     TEXT        NOT NULL,
    category     TEXT        NOT NULL,
    file_path    TEXT        NOT NULL,
    line_start   INTEGER     NOT NULL DEFAULT 0,
    line_end     INTEGER     NOT NULL DEFAULT 0,
    title        TEXT        NOT NULL DEFAULT '',
    rationale    TEXT        NOT NULL DEFAULT '',
    suggestion   TEXT,
    confidence   REAL        NOT NULL DEFAULT 0,
    citations    JSONB       NOT NULL DEFAULT '[]'::jsonb,
    merged_from  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    dedupe_key   TEXT        NOT NULL DEFAULT '',
    posted       BOOLEAN     NOT NULL DEFAULT false,
    thread_id    INTEGER,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS finding_review_idx   ON finding_records (review_id);
CREATE INDEX IF NOT EXISTS finding_severity_idx ON finding_records (severity, created_at DESC);
CREATE INDEX IF NOT EXISTS finding_agent_idx    ON finding_records (agent_type, created_at DESC);

CREATE TABLE IF NOT EXISTS hitl_reviews (
    hitl_id      UUID PRIMARY KEY,
    review_id    UUID        NOT NULL REFERENCES pr_review_records(review_id) ON DELETE CASCADE,
    reason       TEXT        NOT NULL DEFAULT '',
    decision     TEXT        NOT NULL DEFAULT 'pending',
    decided_by   TEXT,
    decided_at   TIMESTAMPTZ,
    note         TEXT,
    -- Which finding ids the human kept. Empty until a decision lands.
    approved_findings JSONB  NOT NULL DEFAULT '[]'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS hitl_pending_idx ON hitl_reviews (decision, created_at DESC);

-- Idempotency: Azure DevOps retries service hook deliveries. The unique key
-- here is what makes a retry a no-op instead of a second review.
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    idempotency_key TEXT PRIMARY KEY,
    notification_id TEXT        NOT NULL DEFAULT '',
    event_type      TEXT        NOT NULL DEFAULT '',
    review_id       UUID,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------------
-- Lane 3: time
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_events (
    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_id       UUID        NOT NULL DEFAULT gen_random_uuid(),
    review_id      UUID,
    trace_id       TEXT        NOT NULL DEFAULT '',
    span_id        TEXT        NOT NULL DEFAULT '',
    parent_span_id TEXT        NOT NULL DEFAULT '',
    agent_type     TEXT        NOT NULL DEFAULT '',
    event_type     TEXT        NOT NULL,
    status         TEXT        NOT NULL DEFAULT 'ok',
    duration_ms    INTEGER     NOT NULL DEFAULT 0,
    model          TEXT        NOT NULL DEFAULT '',
    input_tokens   INTEGER     NOT NULL DEFAULT 0,
    output_tokens  INTEGER     NOT NULL DEFAULT 0,
    payload        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (ts, event_id)
);

CREATE INDEX IF NOT EXISTS agent_events_review_idx ON agent_events (review_id, ts DESC);
CREATE INDEX IF NOT EXISTS agent_events_trace_idx  ON agent_events (trace_id, ts);
CREATE INDEX IF NOT EXISTS agent_events_agent_idx  ON agent_events (agent_type, ts DESC);
CREATE INDEX IF NOT EXISTS agent_events_type_idx   ON agent_events (event_type, ts DESC);

-- {{TIMESCALE}}
SELECT create_hypertable(
    'agent_events', 'ts',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE,
    migrate_data => TRUE
);
-- {{/TIMESCALE}}
