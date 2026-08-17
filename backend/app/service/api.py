"""REST surface consumed by the Next.js dashboard.

Mostly read; the write paths are the HITL decision, cancelling a task, starting
a re-review, and triggering a manual code-memory indexing run - the places a
human directs the system rather than just watching it.

The `/api/index` routes are the only *HTTP* entry point into indexing - a run
only starts through this router when a human hits it directly (today: the
dashboard's Index page). The other entry point is the nightly
`cron_nightly_reindex` worker job in `app.service.queue`, which calls the same
`repositories.create_index_run` / `queue.enqueue_index_run` pair directly,
bypassing this router entirely. Nothing here is ever called by the poller or a
webhook.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.config import settings
from app.contracts.azure import get_ado_client
from app.contracts.models import ReviewOutcome, ReviewRequest
from app.platform import observability as events
from app.platform import repositories as repo
from app.service.queue import abort_job, enqueue_index_run, enqueue_review

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api", tags=["api"])
index_router = APIRouter(prefix="/api/index", tags=["index"])


# --------------------------------------------------------------------- board
@router.get("/board")
async def board(
    repo_id: list[str] = Query(default=[], alias="repo"),
    author: list[str] = Query(default=[]),
    limit: int = Query(200, ge=1, le=500),
) -> dict[str, Any]:
    """Every pull request still needing attention, most recently pushed to
    first.

    Reads only from the database, so it is fast and survives a restart - the
    poller is what keeps it current, not a live call to Azure DevOps on page
    load. Merged/abandoned pull requests and ones already reviewed
    successfully never appear here - Azure DevOps is where that history
    lives, and a fresh commit brings a pull request back on its own.

    The only two filters are repository and author, each multi-select.
    """
    return {
        "pull_requests": await repo.list_board(repo_id, author, limit),
        "total": await repo.board_total(repo_id, author),
    }


@router.get("/board/filters")
async def board_filters() -> dict[str, Any]:
    """Options for the board's repository and author filter dropdowns."""
    return await repo.list_board_filters()


# ------------------------------------------------------------------- reviews
@router.get("/reviews")
async def list_reviews(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    review_status: str | None = Query(None, alias="status"),
) -> dict[str, Any]:
    return {"reviews": await repo.list_reviews(limit, offset, review_status)}


@router.get("/reviews/{review_id}")
async def get_review(review_id: UUID) -> dict[str, Any]:
    review = await repo.get_review(review_id)
    if not review:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Review not found")
    return {"review": review, "findings": await repo.get_findings(review_id)}


@router.get("/reviews/{review_id}/logs")
async def get_logs(review_id: UUID) -> dict[str, Any]:
    """Every span, LLM call and decision for this one task, in order -
    nothing pooled across reviews."""
    if not await repo.get_review(review_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Review not found")
    return {"logs": await events.trace_for_review(review_id)}


class CommentBody(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    author: str = Field(min_length=1, max_length=200)


@router.get("/reviews/{review_id}/comments")
async def list_comments(review_id: UUID) -> dict[str, Any]:
    if not await repo.get_review(review_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Review not found")
    return {"comments": await repo.list_review_comments(review_id)}


@router.post("/reviews/{review_id}/comments", status_code=status.HTTP_201_CREATED)
async def post_comment(review_id: UUID, body: CommentBody) -> dict[str, Any]:
    """Post a free-text comment straight to the real Azure DevOps pull
    request, then mirror it here.

    A failed post is reported as a failure, not silently dropped - a
    reviewer who is told "posted" and finds nothing on the pull request has
    been misled about an irreversible action, same as a failed HITL publish.
    """
    review = await repo.get_review(review_id)
    if not review:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Review not found")

    pr = repo.pr_from_review_row(review)
    client = get_ado_client()
    try:
        thread_id = await client.create_comment_thread(pr, body.content.strip())
    except Exception as exc:  # noqa: BLE001
        log.error("comment.post.failed", review_id=str(review_id), error=str(exc))
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Posting to Azure DevOps failed: {exc}"
        ) from exc
    finally:
        await client.aclose()

    comment = await repo.create_review_comment(
        review_id, body.author.strip(), body.content.strip(), thread_id
    )
    await events.emit(
        "review.comment.posted", review_id=review_id, thread_id=thread_id, author=body.author
    )
    return {"comment": comment}


@router.post("/reviews/{review_id}/cancel")
async def cancel_review(review_id: UUID) -> dict[str, Any]:
    """Stop a queued or running task. No resume - a stopped task is done;
    ask for a fresh review instead."""
    review = await repo.get_review(review_id)
    if not review:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Review not found")

    if not await repo.cancel_review(review_id):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot cancel a review that is already '{review['status']}'",
        )

    # Best-effort: skips a queued job outright. If this misses (already
    # running, or Redis hiccups) the `cancelled` status in Postgres is what
    # actually stops it - the guard in the review graph checks that between
    # every step.
    await abort_job(review_id)
    await events.emit("review.cancel.requested", review_id=review_id)
    return {"review_id": str(review_id), "status": "cancelled"}


# ------------------------------------------------------------ pull requests
@router.post(
    "/pull-requests/{repository_id}/{pull_request_id}/review",
    status_code=status.HTTP_201_CREATED,
)
async def trigger_review(repository_id: str, pull_request_id: int) -> dict[str, Any]:
    """Re-review: a new task for this pull request, whatever its last one did.

    Reuses the same claim -> create -> enqueue path the poller uses for every
    other review, just with an always-unique idempotency key - the point of
    asking for this is a deliberate second opinion, so an existing terminal
    review must never block it.
    """
    tracked = await repo.get_pull_request(repository_id, pull_request_id)
    if not tracked:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "This pull request isn't tracked yet"
        )

    pr = repo.pr_from_pull_request_row(tracked, settings.ado_org_url)
    key = f"manual:{uuid4().hex}"
    review_id = await repo.claim_delivery(key, notification_id="", event_type="manual")
    if review_id is None:  # a fresh uuid-based key never collides in practice
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not start a review")

    request = ReviewRequest(review_id=review_id, pr=pr, idempotency_key=key, event_type="manual")
    await repo.create_review(request)
    await events.emit(
        "review.requested", review_id=review_id, pr=pr.slug, reason="manual re-review"
    )
    await enqueue_review(request)
    return {"review_id": str(review_id), "status": "queued"}


@router.get("/pull-requests/{repository_id}/{pull_request_id}/reviews")
async def review_history(repository_id: str, pull_request_id: int) -> dict[str, Any]:
    """Every review this pull request has had, most recent first."""
    return {"reviews": await repo.list_reviews_for_pull_request(repository_id, pull_request_id)}


# ---------------------------------------------------------------------- HITL
class HitlDecisionBody(BaseModel):
    decision: Literal["approved", "rejected"]
    decided_by: str = Field(min_length=1, max_length=200)
    note: str = ""
    approved_findings: list[str] = Field(
        default_factory=list,
        description="Finding ids to post. Empty means post all of them.",
    )


@router.get("/hitl")
async def list_hitl(
    decision: str = Query("pending"), limit: int = Query(50, ge=1, le=200)
) -> dict[str, Any]:
    return {"queue": await repo.list_hitl(decision, limit)}


@router.post("/hitl/{hitl_id}/decision")
async def decide(hitl_id: UUID, body: HitlDecisionBody) -> dict[str, Any]:
    """Approve or reject a queued review.

    Approving posts to Azure DevOps synchronously, so the caller learns whether
    the post actually worked rather than being told 'approved' and finding out
    later that nothing appeared on the pull request.
    """
    record = await repo.get_hitl(hitl_id)
    if not record:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "HITL item not found")

    claimed = await repo.decide_hitl(
        hitl_id, body.decision, body.decided_by, body.note, body.approved_findings
    )
    if not claimed:
        # Someone else got there first; say so rather than double-posting.
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This item has already been decided"
        )

    review_id = UUID(record["review_id"])
    await events.emit(
        "hitl.decided",
        review_id=review_id,
        hitl_id=str(hitl_id),
        decision=body.decision,
        decided_by=body.decided_by,
    )

    if body.decision == "rejected":
        await repo.set_review_status(review_id, "rejected")
        return {"status": "rejected", "review_id": str(review_id)}

    review = await repo.get_review(review_id)
    if not review:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Review not found")

    findings = await repo.load_findings(review_id)
    if body.approved_findings:
        keep = set(body.approved_findings)
        findings = [f for f in findings if str(f.id) in keep]

    outcome = ReviewOutcome(
        review_id=review_id,
        pr=repo.pr_from_review_row(review),
        status="posted",
        findings=findings,
        overall_confidence=review["overall_confidence"],
        summary=review["summary"],
        duration_ms=review["duration_ms"],
    )

    from app.agent.runner import publish

    client = get_ado_client()
    try:
        await publish(client, outcome)
    except Exception as exc:  # noqa: BLE001
        log.error("hitl.publish.failed", review_id=str(review_id), error=str(exc))
        await repo.set_review_status(review_id, "failed", error=f"publish failed: {exc}")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Approved, but posting to Azure DevOps failed: {exc}"
        ) from exc
    finally:
        await client.aclose()

    await repo.set_review_status(review_id, "posted")
    return {
        "status": "posted",
        "review_id": str(review_id),
        "threads": outcome.posted_thread_ids,
    }


# ----------------------------------------------------------------- code index
class IndexTriggerBody(BaseModel):
    project: str = ""
    # Fallback for a repository the poller hasn't tracked yet - everything
    # else is resolved from the tracked pull_requests row when one exists.
    repository_name: str = ""
    branch: str = ""
    commit_sha: str = ""
    replace: bool = False
    embed: bool = True
    triggered_by: str = ""


@index_router.post("/{repository_id}", status_code=status.HTTP_201_CREATED)
async def trigger_index(repository_id: str, body: IndexTriggerBody) -> dict[str, Any]:
    """Queue one indexing run. Never triggered any other way."""
    info = await repo.repository_info(repository_id)
    project = body.project or (info["project"] if info else "") or settings.ado_project
    repository_name = body.repository_name or (info["repository_name"] if info else "")

    if not repository_name:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This repository isn't tracked yet - pass repository_name explicitly.",
        )
    if not project:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "No project given and ADO_PROJECT is empty."
        )

    run_id = await repo.create_index_run(
        repository_id,
        project,
        repository_name,
        body.branch,
        replace_existing=body.replace,
        embed_requested=body.embed,
        triggered_by=body.triggered_by,
    )
    await enqueue_index_run(
        {
            "run_id": str(run_id),
            "project": project,
            "repository_id": repository_id,
            "branch": body.branch,
            "commit_sha": body.commit_sha,
            "replace_existing": body.replace,
            "embed_requested": body.embed,
        }
    )
    return {"run_id": str(run_id), "status": "queued"}


@index_router.get("")
async def index_overview() -> dict[str, Any]:
    """Every repository the system knows about, with its current index status.

    Chunk/file counts come straight from `code_chunks` on the default vector
    backend. On MEMORY_BACKEND=mem0 that table stays empty, so this fans out
    one live stats call per tracked repository to mem0 instead.
    """
    rows = await repo.list_index_status()
    if settings.memory_backend == "mem0":
        from app.platform.memory import mem0_stats

        for row in rows:
            row.update(await mem0_stats(row["repository_id"]))
    return {"repositories": rows}


@index_router.get("/{repository_id}")
async def index_status(repository_id: str) -> dict[str, Any]:
    row = await repo.index_status_for_repository(repository_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This repository isn't tracked yet")
    if settings.memory_backend == "mem0":
        from app.platform.memory import mem0_stats

        row.update(await mem0_stats(repository_id))
    return row


@index_router.get("/{repository_id}/runs")
async def index_run_history(
    repository_id: str, limit: int = Query(20, ge=1, le=200)
) -> dict[str, Any]:
    return {"runs": await repo.list_index_runs(repository_id, limit)}
