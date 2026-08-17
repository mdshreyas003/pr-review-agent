"""Reads and writes against the truth lane.

The rest of the app talks to these functions, never to raw SQL. Keeping the
queries in one file is what makes the "no cross-module SQL" rule enforceable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from app.contracts.models import (
    Finding,
    PullRequestRef,
    ReviewOutcome,
    ReviewRequest,
    Severity,
)
from app.platform import db

if TYPE_CHECKING:
    from app.platform.memory import IndexResult


# ---------------------------------------------------------------- idempotency
async def claim_delivery(
    idempotency_key: str, notification_id: str, event_type: str
) -> UUID | None:
    """Claim a webhook delivery. Returns None if we've already seen this key.

    ON CONFLICT DO NOTHING is the whole idempotency mechanism: two concurrent
    deliveries race for the same row and exactly one wins.
    """
    review_id = uuid4()
    row = await db.fetchrow(
        """
        INSERT INTO webhook_deliveries (idempotency_key, notification_id, event_type, review_id)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING review_id
        """,
        idempotency_key,
        notification_id,
        event_type,
        review_id,
    )
    return row["review_id"] if row else None


async def existing_review_for_key(idempotency_key: str) -> UUID | None:
    return await db.fetchval(
        "SELECT review_id FROM webhook_deliveries WHERE idempotency_key = $1",
        idempotency_key,
    )


# ------------------------------------------------------------- tracked PRs
async def upsert_pull_request(pr: PullRequestRef) -> None:
    """Record that this pull request exists, reviewed or not.

    Called for every pull request the poller sees, which is what lets the
    dashboard show something that is open but not yet reviewed - and what makes
    the board survive a restart, since it no longer has to re-derive state from
    review records alone.
    """
    await db.execute(
        """
        INSERT INTO pull_requests (
            repository_id, pull_request_id, project, repository_name, title,
            description, author, source_branch, target_branch, source_commit,
            is_draft, web_url, closed_at, last_seen_at, last_commit_at
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,NULL,now(),now())
        ON CONFLICT (repository_id, pull_request_id) DO UPDATE
            SET title           = EXCLUDED.title,
                description     = EXCLUDED.description,
                author          = EXCLUDED.author,
                source_branch   = EXCLUDED.source_branch,
                target_branch   = EXCLUDED.target_branch,
                source_commit   = EXCLUDED.source_commit,
                is_draft        = EXCLUDED.is_draft,
                web_url         = EXCLUDED.web_url,
                closed_at       = NULL,
                last_seen_at    = now(),
                -- Only moves when the commit actually changed - every sweep
                -- touches last_seen_at regardless, which is exactly why that
                -- column can't be used to order "most recently active" (it
                -- ties across every open pull request each sweep).
                last_commit_at  = CASE
                    WHEN pull_requests.source_commit IS DISTINCT FROM EXCLUDED.source_commit
                    THEN now()
                    ELSE pull_requests.last_commit_at
                END
        """,
        pr.repository_id,
        pr.pull_request_id,
        pr.project,
        pr.repository_name,
        pr.title,
        pr.description,
        pr.author,
        pr.source_branch,
        pr.target_branch,
        pr.source_commit,
        pr.is_draft,
        pr.web_url,
    )


async def get_pull_request(repository_id: str, pull_request_id: int) -> dict[str, Any] | None:
    row = await db.fetchrow(
        "SELECT * FROM pull_requests WHERE repository_id = $1 AND pull_request_id = $2",
        repository_id,
        pull_request_id,
    )
    return dict(row) if row else None


async def repository_info(repository_id: str) -> dict[str, Any] | None:
    """project/repository_name for a repository the poller has already seen -
    any one tracked pull request carries both, so the first row is enough."""
    row = await db.fetchrow(
        """
        SELECT repository_id, project, repository_name FROM pull_requests
         WHERE repository_id = $1 LIMIT 1
        """,
        repository_id,
    )
    return dict(row) if row else None


def pr_from_pull_request_row(row: dict[str, Any], organization_url: str) -> PullRequestRef:
    """Hydrate a `PullRequestRef` from the tracked-PR row for a manual re-review.

    `organization_url` isn't tracked on `pull_requests` (it's cosmetic on
    `PullRequestRef` - only used for the `web_url` property, never for the
    actual API calls, which go through the client's own configured base URL),
    so the caller supplies it from settings.
    """
    return PullRequestRef(
        organization_url=organization_url,
        project=row["project"],
        repository_id=row["repository_id"],
        repository_name=row["repository_name"],
        pull_request_id=row["pull_request_id"],
        title=row.get("title", ""),
        description=row.get("description", ""),
        source_branch=row.get("source_branch", ""),
        target_branch=row.get("target_branch", ""),
        author=row.get("author", ""),
        source_commit=row.get("source_commit", ""),
        is_draft=row.get("is_draft", False),
    )


async def close_absent_pull_requests(project: str, seen_ids: list[int]) -> int:
    """Mark pull requests that dropped out of the active sweep as closed.

    Merged and abandoned pull requests stop appearing in the active listing.
    They are flagged rather than deleted so their reviews stay auditable.
    """
    closed = await db.fetchval(
        """
        WITH gone AS (
            UPDATE pull_requests
               SET closed_at = now()
             WHERE project = $1
               AND closed_at IS NULL
               AND NOT (pull_request_id = ANY($2::int[]))
            RETURNING 1
        )
        SELECT count(*) FROM gone
        """,
        project,
        seen_ids,
    )
    return int(closed or 0)


# The pipeline state the board groups on. Derived in SQL so filtering and
# counting are one query rather than post-processing in Python.
_PIPELINE_STATE = """
    CASE
        WHEN l.status IS NULL                              THEN 'open'
        WHEN l.status IN ('queued', 'running')             THEN 'processing'
        WHEN l.status = 'awaiting_approval'                THEN 'awaiting_approval'
        WHEN l.status = 'posted'                           THEN 'posted'
        WHEN l.status = 'rejected'                         THEN 'rejected'
        WHEN l.status = 'cancelled'                        THEN 'cancelled'
        ELSE 'failed'
    END
"""

_BOARD_CTE = f"""
WITH latest AS (
    SELECT DISTINCT ON (r.repository_id, r.pull_request_id)
           r.repository_id, r.pull_request_id, r.review_id, r.status,
           r.overall_confidence, r.requires_human, r.escalated, r.summary,
           r.duration_ms, r.created_at AS reviewed_at
      FROM pr_review_records r
     ORDER BY r.repository_id, r.pull_request_id, r.created_at DESC
),
board AS (
    SELECT p.repository_id, p.pull_request_id, p.project, p.repository_name,
           p.title, p.description, p.author, p.source_branch, p.target_branch,
           p.is_draft, p.web_url, p.closed_at, p.first_seen_at, p.last_seen_at,
           p.last_commit_at,
           l.review_id, l.status AS review_status, l.overall_confidence,
           l.escalated, l.summary, l.reviewed_at,
           {_PIPELINE_STATE} AS pipeline_state,
           COALESCE(
               (SELECT count(*) FROM finding_records f WHERE f.review_id = l.review_id),
               0
           )::int AS finding_count
      FROM pull_requests p
      LEFT JOIN latest l
             ON l.repository_id = p.repository_id
            AND l.pull_request_id = p.pull_request_id
)
"""

# closed_at IS NULL AND pipeline_state <> 'posted' is unconditional, not a
# filter a caller can relax: a merged/abandoned PR or one already reviewed
# successfully has nothing left to act on here - Azure DevOps is where that
# history lives. A fresh commit creates a new, non-posted review and the PR
# reappears on its own.
#
# The only filters a caller can apply are repository and author, each a
# multi-select - an empty array means "no restriction on this dimension".
_BOARD_FILTER = """
    WHERE closed_at IS NULL
      AND pipeline_state <> 'posted'
      AND (cardinality($1::text[]) = 0 OR repository_id = ANY($1::text[]))
      AND (cardinality($2::text[]) = 0 OR author = ANY($2::text[]))
"""


async def list_board(
    repository_ids: list[str] | None = None,
    authors: list[str] | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    rows = await db.fetch(
        f"""
        {_BOARD_CTE}
        SELECT * FROM board
        {_BOARD_FILTER}
        ORDER BY last_commit_at DESC
        LIMIT $3
        """,  # noqa: S608 - all filters are bound parameters
        repository_ids or [],
        authors or [],
        limit,
    )
    return [_board_row(r) for r in rows]


async def board_total(repository_ids: list[str] | None = None, authors: list[str] | None = None) -> int:
    return await db.fetchval(
        f"""
        {_BOARD_CTE}
        SELECT count(*)::int FROM board
        {_BOARD_FILTER}
        """,  # noqa: S608
        repository_ids or [],
        authors or [],
    )


async def list_board_filters() -> dict[str, Any]:
    """Repositories and authors worth offering in the board's two filter
    dropdowns - scoped to the same pull requests the board can ever show, so
    the list never offers a choice that would filter everything out."""
    repos = await db.fetch(
        f"""
        {_BOARD_CTE}
        SELECT DISTINCT repository_id, repository_name FROM board
        WHERE closed_at IS NULL AND pipeline_state <> 'posted'
        ORDER BY repository_name
        """  # noqa: S608
    )
    authors = await db.fetch(
        f"""
        {_BOARD_CTE}
        SELECT DISTINCT author FROM board
        WHERE closed_at IS NULL AND pipeline_state <> 'posted' AND author <> ''
        ORDER BY author
        """  # noqa: S608
    )
    return {
        "repositories": [dict(r) for r in repos],
        "authors": [r["author"] for r in authors],
    }


def _board_row(row) -> dict[str, Any]:
    d = dict(row)
    d["review_id"] = str(d["review_id"]) if d["review_id"] else None
    d["overall_confidence"] = float(d["overall_confidence"] or 0)
    for field in ("first_seen_at", "last_seen_at", "last_commit_at", "reviewed_at", "closed_at"):
        d[field] = d[field].isoformat() if d[field] else None
    # The board renders a one-line preview; the full text stays in the API for
    # search but does not need to cross the wire in a list of 200.
    d["description"] = (d["description"] or "")[:400]
    return d


# -------------------------------------------------------------------- reviews
async def get_review_status(review_id: UUID) -> str | None:
    """Cheap status-only read, used by the cancellation guard between graph steps."""
    return await db.fetchval(
        "SELECT status FROM pr_review_records WHERE review_id = $1", review_id
    )


async def list_reviews_for_pull_request(
    repository_id: str, pull_request_id: int
) -> list[dict[str, Any]]:
    """Every review this pull request has had, most recent first.

    One pull request can be reviewed more than once - each webhook delivery on a
    new commit, or a deliberate re-review, is its own row here.
    """
    rows = await db.fetch(
        """
        SELECT * FROM pr_review_records
         WHERE repository_id = $1 AND pull_request_id = $2
         ORDER BY created_at DESC
        """,
        repository_id,
        pull_request_id,
    )
    return [_review_to_dict(r) for r in rows]


async def cancel_review(review_id: UUID) -> bool:
    """Cancel a queued or running task.

    Returns False when the task was already terminal (or doesn't exist) and so
    could not be cancelled - the caller checks `get_review` to tell those two
    cases apart for the API response.
    """
    row = await db.fetchrow(
        """
        UPDATE pr_review_records
           SET status = 'cancelled', updated_at = now()
         WHERE review_id = $1 AND status IN ('queued', 'running')
        RETURNING review_id
        """,
        review_id,
    )
    return row is not None


async def create_review(request: ReviewRequest) -> None:
    pr = request.pr
    await db.execute(
        """
        INSERT INTO pr_review_records (
            review_id, idempotency_key, organization_url, project, repository_id,
            repository_name, pull_request_id, title, author, source_branch,
            target_branch, source_commit, event_type, status
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,'queued')
        ON CONFLICT (review_id) DO NOTHING
        """,
        request.review_id,
        request.idempotency_key,
        pr.organization_url,
        pr.project,
        pr.repository_id,
        pr.repository_name,
        pr.pull_request_id,
        pr.title,
        pr.author,
        pr.source_branch,
        pr.target_branch,
        pr.source_commit,
        request.event_type,
    )


# A review in one of these states has already been decided. Re-executing it
# would burn five model calls to reach the same answer, and would put a second
# entry in the approval queue for a pull request a human already handled.
TERMINAL_STATUSES = (
    "posted",
    "rejected",
    "awaiting_approval",
    "skipped",
    "cancelled",
)


async def claim_review_for_execution(
    review_id: UUID, stale_after_seconds: int = 900
) -> str | None:
    """Take the execution lease, or report the status that refused it.

    The idempotency row in `webhook_deliveries` only stops a *second enqueue*.
    It says nothing at execution time, and jobs outlive the process: Redis is a
    durable volume, ARQ re-delivers on restart, and `retry_jobs` re-runs
    anything interrupted. Without this an app restart re-reviews work that was
    already finished.

    A row stuck in `running` is reclaimable once the lease goes stale, because
    that means the worker died mid-review rather than still holding it.

    Returns None when the lease was taken, otherwise the blocking status.
    """
    row = await db.fetchrow(
        """
        UPDATE pr_review_records
           SET status = 'running', updated_at = now()
         WHERE review_id = $1
           AND status <> ALL($2::text[])
           AND (status <> 'running' OR updated_at < now() - make_interval(secs => $3))
        RETURNING review_id
        """,
        review_id,
        list(TERMINAL_STATUSES),
        stale_after_seconds,
    )
    if row is not None:
        return None

    current = await db.fetchval(
        "SELECT status FROM pr_review_records WHERE review_id = $1", review_id
    )
    return current or "missing"


async def set_review_status(review_id: UUID, status: str, error: str | None = None) -> None:
    await db.execute(
        """
        UPDATE pr_review_records
           SET status = $2, error = COALESCE($3, error), updated_at = now()
         WHERE review_id = $1
        """,
        review_id,
        status,
        error,
    )


async def save_outcome(outcome: ReviewOutcome) -> None:
    await db.execute(
        """
        UPDATE pr_review_records
           SET status = $2, overall_confidence = $3, requires_human = $4,
               escalated = $5, summary = $6, duration_ms = $7, error = $8,
               updated_at = now()
         WHERE review_id = $1
        """,
        outcome.review_id,
        outcome.status,
        outcome.overall_confidence,
        outcome.requires_human,
        outcome.escalated,
        outcome.summary,
        outcome.duration_ms,
        outcome.error,
    )
    await save_findings(outcome.review_id, outcome.findings)


async def save_findings(review_id: UUID, findings: list[Finding]) -> None:
    if not findings:
        return
    rows = [
        (
            f.id,
            review_id,
            f.agent_type,
            f.severity.value,
            f.category,
            f.file_path,
            f.line_start,
            f.line_end,
            f.title,
            f.rationale,
            f.suggestion,
            f.confidence,
            f.citations,
            f.merged_from,
            f.dedupe_key,
        )
        for f in findings
    ]
    async with db.get_pool().acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO finding_records (
                finding_id, review_id, agent_type, severity, category, file_path,
                line_start, line_end, title, rationale, suggestion, confidence,
                citations, merged_from, dedupe_key
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
            ON CONFLICT (finding_id) DO NOTHING
            """,
            rows,
        )


async def mark_finding_posted(finding_id: UUID, thread_id: int | None) -> None:
    await db.execute(
        "UPDATE finding_records SET posted = true, thread_id = $2 WHERE finding_id = $1",
        finding_id,
        thread_id,
    )


async def get_review(review_id: UUID) -> dict[str, Any] | None:
    row = await db.fetchrow(
        "SELECT * FROM pr_review_records WHERE review_id = $1", review_id
    )
    return _review_to_dict(row) if row else None


async def list_reviews(
    limit: int = 50, offset: int = 0, status: str | None = None
) -> list[dict[str, Any]]:
    if status:
        rows = await db.fetch(
            """
            SELECT * FROM pr_review_records WHERE status = $1
            ORDER BY created_at DESC LIMIT $2 OFFSET $3
            """,
            status,
            limit,
            offset,
        )
    else:
        rows = await db.fetch(
            "SELECT * FROM pr_review_records ORDER BY created_at DESC LIMIT $1 OFFSET $2",
            limit,
            offset,
        )
    return [_review_to_dict(r) for r in rows]


async def get_findings(review_id: UUID) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """
        SELECT * FROM finding_records WHERE review_id = $1
        ORDER BY CASE severity
                     WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1
                     WHEN 'MEDIUM' THEN 2 WHEN 'LOW' THEN 3 ELSE 4 END,
                 confidence DESC
        """,
        review_id,
    )
    return [_finding_to_dict(r) for r in rows]


async def load_findings(review_id: UUID) -> list[Finding]:
    return [
        Finding(
            id=UUID(d["finding_id"]),
            agent_type=d["agent_type"],
            severity=Severity(d["severity"]),
            category=d["category"],
            file_path=d["file_path"],
            line_start=d["line_start"],
            line_end=d["line_end"],
            title=d["title"],
            rationale=d["rationale"],
            suggestion=d["suggestion"],
            confidence=d["confidence"],
            citations=d["citations"],
            merged_from=d["merged_from"],
        )
        for d in await get_findings(review_id)
    ]


# ------------------------------------------------------------------ comments
async def create_review_comment(
    review_id: UUID, author: str, content: str, thread_id: int | None
) -> dict[str, Any]:
    row = await db.fetchrow(
        """
        INSERT INTO review_comments (comment_id, review_id, author, content, thread_id)
        VALUES ($1,$2,$3,$4,$5)
        RETURNING *
        """,
        uuid4(),
        review_id,
        author,
        content,
        thread_id,
    )
    return _comment_to_dict(row)


async def list_review_comments(review_id: UUID) -> list[dict[str, Any]]:
    rows = await db.fetch(
        "SELECT * FROM review_comments WHERE review_id = $1 ORDER BY created_at",
        review_id,
    )
    return [_comment_to_dict(r) for r in rows]


# ----------------------------------------------------------------------- HITL
async def create_hitl(review_id: UUID, reason: str) -> UUID:
    hitl_id = uuid4()
    await db.execute(
        "INSERT INTO hitl_reviews (hitl_id, review_id, reason) VALUES ($1,$2,$3)",
        hitl_id,
        review_id,
        reason,
    )
    return hitl_id


async def list_hitl(decision: str = "pending", limit: int = 50) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """
        SELECT h.hitl_id, h.review_id, h.reason, h.decision, h.decided_by,
               h.decided_at, h.note, h.created_at,
               r.project, r.repository_name, r.pull_request_id, r.title,
               r.author, r.overall_confidence, r.escalated,
               (SELECT count(*) FROM finding_records f
                 WHERE f.review_id = h.review_id) AS finding_count
        FROM hitl_reviews h
        JOIN pr_review_records r USING (review_id)
        WHERE h.decision = $1
        ORDER BY r.escalated DESC, h.created_at DESC
        LIMIT $2
        """,
        decision,
        limit,
    )
    out = []
    for r in rows:
        d = dict(r)
        d["hitl_id"] = str(d["hitl_id"])
        d["review_id"] = str(d["review_id"])
        d["created_at"] = d["created_at"].isoformat()
        d["decided_at"] = d["decided_at"].isoformat() if d["decided_at"] else None
        out.append(d)
    return out


async def get_hitl(hitl_id: UUID) -> dict[str, Any] | None:
    row = await db.fetchrow("SELECT * FROM hitl_reviews WHERE hitl_id = $1", hitl_id)
    if not row:
        return None
    d = dict(row)
    d["hitl_id"] = str(d["hitl_id"])
    d["review_id"] = str(d["review_id"])
    d["created_at"] = d["created_at"].isoformat()
    d["decided_at"] = d["decided_at"].isoformat() if d["decided_at"] else None
    return d


async def decide_hitl(
    hitl_id: UUID,
    decision: str,
    decided_by: str,
    note: str = "",
    approved_findings: list[str] | None = None,
) -> bool:
    """Record a decision. Returns False if someone already decided this one."""
    row = await db.fetchrow(
        """
        UPDATE hitl_reviews
           SET decision = $2, decided_by = $3, note = $4,
               approved_findings = $5, decided_at = now()
         WHERE hitl_id = $1 AND decision = 'pending'
        RETURNING hitl_id
        """,
        hitl_id,
        decision,
        decided_by,
        note,
        approved_findings or [],
        )
    return row is not None


# ---------------------------------------------------------- code index runs
# Triggered manually via app/service/api.py, or nightly via the
# `cron_nightly_reindex` job in app/service/queue.py. Never by a poller or
# webhook.
async def create_index_run(
    repository_id: str,
    project: str,
    repository_name: str,
    branch: str,
    *,
    replace_existing: bool,
    embed_requested: bool,
    triggered_by: str = "",
) -> UUID:
    run_id = uuid4()
    await db.execute(
        """
        INSERT INTO index_runs (
            run_id, repository_id, project, repository_name, branch,
            replace_existing, embed_requested, triggered_by
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        """,
        run_id,
        repository_id,
        project,
        repository_name,
        branch,
        replace_existing,
        embed_requested,
        triggered_by,
    )
    return run_id


async def mark_index_run_running(run_id: UUID, branch: str, commit_sha: str) -> None:
    """Also (re)writes `branch`, since a blank request resolves to the
    repository's default branch only once the job actually runs."""
    await db.execute(
        """
        UPDATE index_runs
           SET status = 'running', branch = $2, commit_sha = $3, started_at = now()
         WHERE run_id = $1
        """,
        run_id,
        branch,
        commit_sha,
    )


async def complete_index_run(
    run_id: UUID,
    result: IndexResult,
    *,
    files_collected: int,
    embed_effective: bool,
) -> None:
    await db.execute(
        """
        UPDATE index_runs
           SET status = 'completed', files_collected = $2, written = $3,
               unchanged = $4, superseded = $5, removed_files = $6,
               embed_effective = $7, finished_at = now()
         WHERE run_id = $1
        """,
        run_id,
        files_collected,
        result.written,
        result.unchanged,
        result.superseded,
        result.removed_files,
        embed_effective,
    )


async def fail_index_run(run_id: UUID, error: str) -> None:
    await db.execute(
        """
        UPDATE index_runs SET status = 'failed', error = $2, finished_at = now()
         WHERE run_id = $1
        """,
        run_id,
        error[:2000],
    )


async def get_index_run(run_id: UUID) -> dict[str, Any] | None:
    row = await db.fetchrow("SELECT * FROM index_runs WHERE run_id = $1", run_id)
    return _index_run_to_dict(row) if row else None


async def list_index_runs(repository_id: str, limit: int = 20) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """
        SELECT * FROM index_runs WHERE repository_id = $1
         ORDER BY created_at DESC LIMIT $2
        """,
        repository_id,
        limit,
    )
    return [_index_run_to_dict(r) for r in rows]


_INDEX_STATUS_CTE = """
WITH latest_run AS (
    SELECT DISTINCT ON (repository_id)
           run_id, repository_id, branch, status, error, created_at, finished_at
      FROM index_runs
     ORDER BY repository_id, created_at DESC
),
chunk_stats AS (
    SELECT repository_id,
           count(*)::int AS chunk_count,
           count(DISTINCT file_path)::int AS file_count,
           max(indexed_at) AS last_indexed_at
      FROM code_chunks
     GROUP BY repository_id
)
"""


async def index_status_for_repository(repository_id: str) -> dict[str, Any] | None:
    """Chunk/file counts here are the vector backend's own numbers
    (`code_chunks`). When MEMORY_BACKEND=mem0 they're 0/never - the caller
    (app.service.api) overwrites them with `memory.mem0_stats(repository_id)`
    in that case, since mem0 keeps its own count, not this table."""
    row = await db.fetchrow(
        f"""
        {_INDEX_STATUS_CTE}
        SELECT p.repository_id, p.project, p.repository_name,
               COALESCE(c.chunk_count, 0) AS chunk_count,
               COALESCE(c.file_count, 0) AS file_count,
               c.last_indexed_at,
               l.run_id AS last_run_id, l.branch AS last_run_branch,
               l.status AS last_run_status, l.error AS last_run_error,
               l.created_at AS last_run_created_at, l.finished_at AS last_run_finished_at
          FROM (
                SELECT DISTINCT repository_id, project, repository_name
                  FROM pull_requests WHERE repository_id = $1
               ) p
          LEFT JOIN chunk_stats c ON c.repository_id = p.repository_id
          LEFT JOIN latest_run l ON l.repository_id = p.repository_id
        """,  # noqa: S608 - no interpolated user input, only the fixed CTE text
        repository_id,
    )
    return _index_status_row(row) if row else None


async def list_index_status() -> list[dict[str, Any]]:
    """Same caveat as `index_status_for_repository` re: MEMORY_BACKEND=mem0."""
    rows = await db.fetch(
        f"""
        {_INDEX_STATUS_CTE}
        SELECT p.repository_id, p.project, p.repository_name,
               COALESCE(c.chunk_count, 0) AS chunk_count,
               COALESCE(c.file_count, 0) AS file_count,
               c.last_indexed_at,
               l.run_id AS last_run_id, l.branch AS last_run_branch,
               l.status AS last_run_status, l.error AS last_run_error,
               l.created_at AS last_run_created_at, l.finished_at AS last_run_finished_at
          FROM (SELECT DISTINCT repository_id, project, repository_name FROM pull_requests) p
          LEFT JOIN chunk_stats c ON c.repository_id = p.repository_id
          LEFT JOIN latest_run l ON l.repository_id = p.repository_id
         ORDER BY p.repository_name
        """  # noqa: S608
    )
    return [_index_status_row(r) for r in rows]


def _index_run_to_dict(row) -> dict[str, Any]:
    d = dict(row)
    d["run_id"] = str(d["run_id"])
    for field in ("created_at", "started_at", "finished_at"):
        d[field] = d[field].isoformat() if d[field] else None
    return d


def _index_status_row(row) -> dict[str, Any]:
    d = dict(row)
    last_run: dict[str, Any] | None = None
    if d["last_run_id"]:
        last_run = {
            "run_id": str(d["last_run_id"]),
            "status": d["last_run_status"],
            "branch": d["last_run_branch"],
            "error": d["last_run_error"],
            "created_at": (
                d["last_run_created_at"].isoformat() if d["last_run_created_at"] else None
            ),
            "finished_at": (
                d["last_run_finished_at"].isoformat() if d["last_run_finished_at"] else None
            ),
        }
    return {
        "repository_id": d["repository_id"],
        "project": d["project"],
        "repository_name": d["repository_name"],
        "chunk_count": d["chunk_count"],
        "file_count": d["file_count"],
        "last_indexed_at": d["last_indexed_at"].isoformat() if d["last_indexed_at"] else None,
        "last_run": last_run,
    }


# ------------------------------------------------------------------- mappers
def _review_to_dict(row) -> dict[str, Any]:
    d = dict(row)
    d["review_id"] = str(d["review_id"])
    d["created_at"] = d["created_at"].isoformat()
    d["updated_at"] = d["updated_at"].isoformat()
    return d


def _finding_to_dict(row) -> dict[str, Any]:
    d = dict(row)
    d["finding_id"] = str(d["finding_id"])
    d["review_id"] = str(d["review_id"])
    d["created_at"] = d["created_at"].isoformat()
    return d


def _comment_to_dict(row) -> dict[str, Any]:
    d = dict(row)
    d["comment_id"] = str(d["comment_id"])
    d["review_id"] = str(d["review_id"])
    d["created_at"] = d["created_at"].isoformat()
    return d


def pr_from_review_row(row: dict[str, Any]) -> PullRequestRef:
    return PullRequestRef(
        organization_url=row["organization_url"],
        project=row["project"],
        repository_id=row["repository_id"],
        repository_name=row["repository_name"],
        pull_request_id=row["pull_request_id"],
        title=row.get("title", ""),
        author=row.get("author", ""),
        source_branch=row.get("source_branch", ""),
        target_branch=row.get("target_branch", ""),
        source_commit=row.get("source_commit", ""),
    )
