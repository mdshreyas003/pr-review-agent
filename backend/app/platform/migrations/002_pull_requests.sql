-- ============================================================================
-- Every pull request the poller has seen, reviewed or not.
--
-- Until now the system only knew about pull requests it had *reviewed*, so a
-- freshly opened one was invisible until a review finished, and a restart left
-- the dashboard with no idea what was outstanding. This is the tracking table
-- behind the unified view: the poller upserts everything it sees each sweep,
-- and the dashboard joins it against the latest review.
--
-- Keyed on (repository_id, pull_request_id) because pull request numbers are
-- only unique within a repository - two repos in the same project both have a
-- pull request 1.
-- ============================================================================

CREATE TABLE IF NOT EXISTS pull_requests (
    repository_id    TEXT        NOT NULL,
    pull_request_id  INTEGER     NOT NULL,
    project          TEXT        NOT NULL DEFAULT '',
    repository_name  TEXT        NOT NULL DEFAULT '',
    title            TEXT        NOT NULL DEFAULT '',
    description      TEXT        NOT NULL DEFAULT '',
    author           TEXT        NOT NULL DEFAULT '',
    source_branch    TEXT        NOT NULL DEFAULT '',
    target_branch    TEXT        NOT NULL DEFAULT '',
    source_commit    TEXT        NOT NULL DEFAULT '',
    is_draft         BOOLEAN     NOT NULL DEFAULT false,
    web_url          TEXT        NOT NULL DEFAULT '',
    -- Set when the pull request stops appearing in an active sweep, which is
    -- how a merged or abandoned one leaves the board without being deleted.
    closed_at        TIMESTAMPTZ,
    first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Bumped only when source_commit itself changes (set in
    -- upsert_pull_request), unlike last_seen_at which ticks on every sweep
    -- whether or not anything changed. This is what the board orders and
    -- filters "recent" by.
    last_commit_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (repository_id, pull_request_id)
);

CREATE INDEX IF NOT EXISTS pull_requests_seen_idx
    ON pull_requests (last_seen_at DESC);
CREATE INDEX IF NOT EXISTS pull_requests_open_idx
    ON pull_requests (closed_at) WHERE closed_at IS NULL;
CREATE INDEX IF NOT EXISTS pull_requests_last_commit_idx
    ON pull_requests (last_commit_at DESC);

-- Search runs over title and description together. trigram rather than
-- tsvector: reviewers search for fragments of identifiers and ticket keys
-- ("GDE-41", "MeasurementAdapter"), which stemming would mangle.
CREATE INDEX IF NOT EXISTS pull_requests_search_idx
    ON pull_requests USING GIN ((title || ' ' || description) gin_trgm_ops);
