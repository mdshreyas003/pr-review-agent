"""Handing work off to the worker, and the ARQ worker process itself.

The queue boundary is deliberately thin: one function, one serialisable
payload per job. Anything that needs to survive a worker restart has already
been written to Postgres by the trigger, so the queue only carries a pointer
plus enough context to avoid a re-fetch.

Run the worker with:  arq app.service.queue.WorkerSettings

Jobs that exhaust their retries land in the dead-letter table rather than
vanishing, because a review that silently never happened is worse than one
that visibly failed - a developer waiting on a review needs to know it isn't
coming.
"""

from __future__ import annotations

from datetime import timedelta, timezone
from typing import Any
from uuid import UUID

import structlog
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from arq.cron import cron

from app.config import settings
from app.contracts.models import ReviewRequest
from app.platform import db
from app.platform import observability as events
from app.platform import repositories as repo
from app.platform.observability import configure_logging

log = structlog.get_logger(__name__)

QUEUE_NAME = "pr_review"
MAX_TRIES = 3

# India has one offset year-round (no DST), so a fixed UTC+5:30 is exact -
# unlike UTC-relative cron hours, this needs no adjustment for where the
# worker container happens to run.
IST = timezone(timedelta(hours=5, minutes=30))

_pool: ArqRedis | None = None


def redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(settings.redis_url)


async def get_queue() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(redis_settings())
    return _pool


async def close_queue() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


# ------------------------------------------------------------------- enqueue
async def enqueue_review(request: ReviewRequest) -> str | None:
    payload: dict[str, Any] = request.model_dump(mode="json")

    if settings.queue_mode == "inline":
        from app.agent.runner import run_review

        await run_review(request)
        return None

    queue = await get_queue()
    job = await queue.enqueue_job(
        "review_pull_request",
        payload,
        _job_id=str(request.review_id),  # ARQ dedupes on job id, belt and braces
        _queue_name=QUEUE_NAME,
    )
    log.info("queue.enqueued", review_id=str(request.review_id), job_id=job.job_id if job else None)
    return job.job_id if job else None


async def enqueue_index_run(payload: dict[str, Any]) -> str | None:
    """Hand off one manual indexing run. Same thin boundary as `enqueue_review`."""
    if settings.queue_mode == "inline":
        from app.platform.memory import run_index

        await run_index(
            UUID(payload["run_id"]),
            payload["project"],
            payload["repository_id"],
            payload.get("branch", ""),
            replace_existing=payload.get("replace_existing", False),
            embed_requested=payload.get("embed_requested", True),
            commit_sha=payload.get("commit_sha", ""),
        )
        return None

    queue = await get_queue()
    job = await queue.enqueue_job(
        "index_repository",
        payload,
        _job_id=f"index:{payload['run_id']}",
        _queue_name=QUEUE_NAME,
    )
    log.info(
        "queue.index.enqueued",
        run_id=payload.get("run_id"),
        job_id=job.job_id if job else None,
    )
    return job.job_id if job else None


async def abort_job(review_id: UUID) -> bool:
    """Best-effort: skip a queued job outright instead of letting the worker
    start it only to hit the cancellation guard.

    The job id is always the review id (see `enqueue_review`), so this needs
    no lookup. Not the correctness mechanism for cancellation - that's the
    `cancelled` status in Postgres, which the worker honours even if this
    abort never reaches Redis in time or the job already started.
    """
    if settings.queue_mode == "inline":
        return False
    from arq.jobs import Job

    queue = await get_queue()
    try:
        # Short timeout: if the job is already running, waiting for arq to
        # confirm the abort would block the API response on however long the
        # review takes. The DB-level `cancelled` status is what actually
        # guarantees the task stops - this is purely a faster path for the
        # common case of cancelling something still queued.
        return await Job(str(review_id), queue, _queue_name=QUEUE_NAME).abort(timeout=2.0)
    except Exception as exc:  # noqa: BLE001
        log.warning("queue.abort.failed", review_id=str(review_id), error=str(exc))
        return False


# --------------------------------------------------------------------- worker
async def review_pull_request(ctx: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from app.agent.runner import run_review

    request = ReviewRequest.model_validate(payload)
    attempt = int(ctx.get("job_try", 1))
    log.info(
        "worker.job.start",
        review_id=str(request.review_id),
        pr=request.pr.slug,
        attempt=attempt,
    )
    try:
        outcome = await run_review(request)
    except Exception as exc:  # noqa: BLE001
        # run_review handles its own domain failures; reaching here means the
        # process itself was in trouble (timeout, cancellation, OOM recovery).
        if attempt >= MAX_TRIES:
            await _dead_letter(request.review_id, attempt, exc)
        raise
    return {
        "review_id": str(outcome.review_id),
        "status": outcome.status,
        "findings": len(outcome.findings),
    }


async def index_repository(ctx: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.memory import run_index

    run_id = UUID(payload["run_id"])
    attempt = int(ctx.get("job_try", 1))
    log.info(
        "worker.index.start",
        run_id=str(run_id),
        repository_id=payload["repository_id"],
        attempt=attempt,
    )
    try:
        await run_index(
            run_id,
            payload["project"],
            payload["repository_id"],
            payload.get("branch", ""),
            replace_existing=payload.get("replace_existing", False),
            embed_requested=payload.get("embed_requested", True),
            commit_sha=payload.get("commit_sha", ""),
        )
    except Exception as exc:  # noqa: BLE001
        # run_index already records its own failures; reaching here on the
        # final attempt with the run somehow still not marked terminal is the
        # same defence-in-depth as the review worker's dead-letter below.
        if attempt >= MAX_TRIES:
            await _fail_stuck_index_run(run_id, exc)
        raise
    return {"run_id": str(run_id), "status": "completed"}


async def cron_nightly_reindex(ctx: dict[str, Any]) -> None:
    """Worker-scheduled job: reconcile the code index for every tracked repository.

    This is the one place indexing is ever triggered by something other than a
    human hitting `/api/index` (see that router's docstring). It reuses the
    exact same path a manual run takes - `create_index_run` then
    `enqueue_index_run` - so a nightly pass is indistinguishable from a manual
    one except for `triggered_by`, and gets the same retry/dead-letter handling
    from `index_repository`.

    `replace_existing` is always False: `run_index` already reconciles by
    content hash and prunes files missing from the branch, so a nightly pass
    only touches what actually changed. A full wipe stays a deliberate manual
    choice, never something a scheduled job does on its own.

    Scheduled for 00:00 IST - see `cron(..., hour=0, minute=0)` and
    `WorkerSettings.timezone` below.
    """
    if not settings.nightly_reindex_enabled:
        return

    repositories = await repo.list_index_status()
    if settings.ado_repositories:
        # Same restriction as the poller (ADO_REPOSITORIES): don't spend a
        # nightly reindex pass on a repository nobody asked to have reviewed.
        wanted = set(settings.ado_repositories)
        repositories = [
            r for r in repositories
            if r["repository_id"] in wanted or r["repository_name"] in wanted
        ]
    log.info("worker.cron.nightly_reindex.start", repositories=len(repositories))

    queued = 0
    for row in repositories:
        try:
            run_id = await repo.create_index_run(
                row["repository_id"],
                row["project"],
                row["repository_name"],
                "",  # blank branch resolves to the repository's default branch
                replace_existing=False,
                embed_requested=True,
                triggered_by="nightly-cron",
            )
            await enqueue_index_run(
                {
                    "run_id": str(run_id),
                    "project": row["project"],
                    "repository_id": row["repository_id"],
                    "branch": "",
                    "commit_sha": "",
                    "replace_existing": False,
                    "embed_requested": True,
                }
            )
            queued += 1
        except Exception as exc:  # noqa: BLE001
            # One repository failing to queue shouldn't stop the rest of the
            # fleet from being reconciled tonight.
            log.warning(
                "worker.cron.nightly_reindex.repo_failed",
                repository_id=row.get("repository_id"),
                error=str(exc)[:200],
            )

    log.info(
        "worker.cron.nightly_reindex.done", repositories=len(repositories), queued=queued
    )


async def _fail_stuck_index_run(run_id: UUID, exc: BaseException) -> None:
    try:
        current = await repo.get_index_run(run_id)
        if current and current["status"] not in ("completed", "failed"):
            await repo.fail_index_run(run_id, f"{type(exc).__name__}: {exc}")
    except Exception as inner:  # noqa: BLE001
        log.error("worker.index.dead_letter.failed", run_id=str(run_id), error=str(inner))


async def _dead_letter(review_id: UUID, attempts: int, exc: BaseException) -> None:
    """Last attempt failed: make the review visibly dead rather than eternally 'running'."""
    try:
        await repo.set_review_status(review_id, "failed", error=f"{type(exc).__name__}: {exc}")
        await events.emit(
            "job.dead_letter",
            review_id=review_id,
            status="error",
            attempts=attempts,
            error=str(exc)[:500],
        )
    except Exception as inner:  # noqa: BLE001
        log.error("worker.dead_letter.failed", review_id=str(review_id), error=str(inner))


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging()
    await db.init_pool()
    log.info("worker.started", queue=QUEUE_NAME)


async def shutdown(ctx: dict[str, Any]) -> None:
    await db.close_pool()
    log.info("worker.stopped")


class WorkerSettings:
    functions = [review_pull_request, index_repository]
    cron_jobs = [
        cron(cron_nightly_reindex, hour=0, minute=0, run_at_startup=False)
    ]
    # arq evaluates cron fields (hour=0, minute=0 above) against this
    # timezone, defaulting otherwise to whatever the host system clock is set
    # to - explicit here so "midnight" means midnight IST regardless of the
    # worker container's own timezone.
    timezone = IST
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = redis_settings()
    queue_name = QUEUE_NAME
    max_jobs = 5
    job_timeout = settings.agent_timeout_seconds * 3
    max_tries = MAX_TRIES
    retry_jobs = True
    keep_result = 3600
    health_check_interval = 30
