"""The single entry point for "review this pull request", and posting the result.

`run_review` owns the things that must happen exactly once around a review
regardless of who triggered it: bind a trace, mark the record running,
guarantee the Azure DevOps client is closed, and guarantee the review never
ends in `running` no matter how it failed.

`publish` turns findings into Azure DevOps comment threads. Posting is the only
irreversible thing this system does, so it is the one place that checks what is
already there - re-running a review on an updated PR must not duplicate a
comment the author has already read and possibly already replied to.
"""

from __future__ import annotations

import structlog

from app.agent.engine import execute
from app.config import settings
from app.contracts.azure import AzureDevOpsClient, get_ado_client
from app.contracts.models import Finding, PullRequestRef, ReviewOutcome, ReviewRequest
from app.platform import observability as events
from app.platform import repositories as repo

log = structlog.get_logger(__name__)

MARKER = "<!-- ai-pr-review -->"


async def run_review(request: ReviewRequest) -> ReviewOutcome:
    events.bind_trace(events.new_trace_id(), request.review_id)

    # Execution-time guard. The enqueue-time claim stops a second *job* being
    # created; this stops a job that already ran from running again after a
    # restart, an ARQ retry, or a re-delivery.
    blocked_by = await repo.claim_review_for_execution(request.review_id)
    if blocked_by is not None:
        log.info(
            "review.skipped.already_decided",
            review_id=str(request.review_id),
            pr=request.pr.slug,
            status=blocked_by,
        )
        await events.emit(
            "review.skipped",
            review_id=request.review_id,
            pr=request.pr.slug,
            reason=f"already {blocked_by}",
        )
        return await _existing_outcome(request, blocked_by)

    await events.emit(
        "review.started",
        review_id=request.review_id,
        pr=request.pr.slug,
        event_type_name=request.event_type,
    )

    client = get_ado_client()
    try:
        outcome = await execute(request, client)
    except Exception as exc:  # noqa: BLE001
        # Anything reaching here is a bug or an infrastructure failure. Record
        # it as failed so the review stops looking like it is still working,
        # then re-raise so the worker's retry policy sees it.
        log.exception("review.failed", review_id=str(request.review_id), pr=request.pr.slug)
        await repo.set_review_status(
            request.review_id, "failed", error=f"{type(exc).__name__}: {exc}"
        )
        await events.emit(
            "review.failed",
            review_id=request.review_id,
            status="error",
            error=str(exc)[:500],
            error_type=type(exc).__name__,
        )
        raise
    finally:
        await client.aclose()

    await events.emit(
        "review.completed",
        review_id=request.review_id,
        terminal_status=outcome.status,
        findings=len(outcome.findings),
        confidence=outcome.overall_confidence,
        duration_ms=outcome.duration_ms,
    )
    log.info(
        "review.completed",
        review_id=str(request.review_id),
        pr=request.pr.slug,
        status=outcome.status,
        findings=len(outcome.findings),
    )
    return outcome


async def _existing_outcome(request: ReviewRequest, status: str) -> ReviewOutcome:
    """Rebuild the decided outcome so callers get a result, not an exception."""
    record = await repo.get_review(request.review_id)
    if record is None:
        return ReviewOutcome(
            review_id=request.review_id,
            pr=request.pr,
            status="failed",  # type: ignore[arg-type]
            error="review record is missing",
        )
    return ReviewOutcome(
        review_id=request.review_id,
        pr=request.pr,
        status=status,  # type: ignore[arg-type]
        findings=await repo.load_findings(request.review_id),
        overall_confidence=record["overall_confidence"],
        requires_human=record["requires_human"],
        escalated=record["escalated"],
        summary=record["summary"],
        duration_ms=record["duration_ms"],
        error=record["error"],
    )


# ------------------------------------------------------------------- publish
def summary_comment(outcome: ReviewOutcome) -> str:
    counts: dict[str, int] = {}
    for f in outcome.findings:
        counts[f.severity.value] = counts.get(f.severity.value, 0) + 1
    breakdown = " · ".join(f"{v} {k}" for k, v in counts.items()) or "no findings"

    agents = ", ".join(
        f"{r.agent_type}{'' if r.ok else ' (failed)'}" for r in outcome.agent_results
    )
    return "\n".join(
        [
            MARKER,
            "## 🤖 Automated review",
            "",
            outcome.summary.strip(),
            "",
            f"**{breakdown}** · confidence {outcome.overall_confidence:.2f} "
            f"· {outcome.duration_ms}ms",
            "",
            f"<sub>Specialists: {agents}.</sub>",
        ]
    )


def _fingerprint(finding: Finding) -> str:
    """Stable per-finding marker, so a re-review can recognise its own comments."""
    return f"<!-- ai-pr-review:{finding.dedupe_key} -->"


async def already_posted(client: AzureDevOpsClient, pr: PullRequestRef) -> set[str]:
    """Fingerprints of findings this system has already commented on."""
    try:
        threads = await client.list_comment_threads(pr)
    except Exception as exc:  # noqa: BLE001
        log.warning("publish.existing_threads.unavailable", pr=pr.slug, error=str(exc))
        return set()

    seen: set[str] = set()
    for thread in threads:
        for comment in thread.get("comments") or []:
            content = comment.get("content") or ""
            if "<!-- ai-pr-review:" not in content:
                continue
            start = content.index("<!-- ai-pr-review:") + len("<!-- ai-pr-review:")
            end = content.find("-->", start)
            if end > start:
                seen.add(content[start:end].strip())
    return seen


async def publish(
    client: AzureDevOpsClient, outcome: ReviewOutcome
) -> ReviewOutcome:
    """Post the summary plus one thread per finding. Idempotent across re-runs."""
    pr = outcome.pr
    posted_fingerprints = await already_posted(client, pr)
    thread_ids: list[int] = []

    async with events.span("publish", review_id=outcome.review_id, pr=pr.slug) as span:
        summary_id = await client.create_comment_thread(
            pr, summary_comment(outcome), status="closed"
        )
        if summary_id:
            thread_ids.append(summary_id)

        skipped = 0
        for finding in outcome.findings[: settings.max_findings_posted]:
            fingerprint = finding.dedupe_key
            if fingerprint in posted_fingerprints:
                skipped += 1
                continue
            body = f"{_fingerprint(finding)}\n{finding.to_markdown()}"
            try:
                thread_id = await client.create_comment_thread(
                    pr,
                    body,
                    file_path=finding.file_path or None,
                    line=finding.line_start or None,
                )
            except Exception as exc:  # noqa: BLE001
                # One rejected thread must not cost the rest of the review.
                log.warning(
                    "publish.thread.failed",
                    pr=pr.slug,
                    path=finding.file_path,
                    error=str(exc)[:200],
                )
                continue
            if thread_id:
                thread_ids.append(thread_id)
            await repo.mark_finding_posted(finding.id, thread_id)

        span.update(threads=len(thread_ids), skipped_duplicates=skipped)

    outcome.posted_thread_ids = thread_ids
    log.info(
        "publish.done", pr=pr.slug, threads=len(thread_ids), review_id=str(outcome.review_id)
    )
    return outcome
