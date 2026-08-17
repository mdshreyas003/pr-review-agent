-- ============================================================================
-- Ad-hoc comments posted from the dashboard, distinct from agent findings.
--
-- A reviewer can leave a free-text note on a review from within the app; it
-- is posted to the real Azure DevOps pull request as a top-level thread
-- (see AzureDevOpsClient.create_comment_thread) and mirrored here so the
-- dashboard doesn't need to round-trip to ADO just to render the thread back.
-- ============================================================================

CREATE TABLE IF NOT EXISTS review_comments (
    comment_id   UUID        PRIMARY KEY,
    review_id    UUID        NOT NULL REFERENCES pr_review_records(review_id) ON DELETE CASCADE,
    author       TEXT        NOT NULL DEFAULT '',
    content      TEXT        NOT NULL,
    thread_id    INTEGER,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS review_comments_review_idx ON review_comments (review_id, created_at);
