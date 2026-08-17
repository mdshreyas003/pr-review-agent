"""The two ways work enters the system: a webhook push, or a poll sweep.

Azure DevOps service hooks differ from GitHub webhooks in two ways that matter
here. There is no HMAC signature - subscriptions authenticate with HTTP Basic
credentials you set when creating them - and deliveries are retried on non-2xx,
which is why the idempotency key is load-bearing rather than defensive. This
ingress never does review work: it parses, records, and hands off, so Azure
DevOps sees a fast 200 regardless of how long the five specialists take.

The poller is the alternative for a laptop or anywhere Azure DevOps cannot
reach inbound: it asks what is open and reviews anything not yet seen. It is
not a parallel implementation - it builds the same `PullRequestRef`, derives
the same idempotency key, and claims work through the same `webhook_deliveries`
row, so the two triggers can run side by side and a pull request is still
reviewed exactly once. The trade-off against a webhook is latency: a review
starts within one poll interval instead of immediately.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Header, HTTPException, Request, status

from app.config import settings
from app.contracts.azure import (
    AzureDevOpsClient,
    get_ado_client,
    idempotency_key_for,
    pull_request_ref_from_resource,
)
from app.contracts.models import PullRequestRef, ReviewRequest
from app.platform import observability as events
from app.platform import repositories as repo
from app.service.queue import enqueue_review

log = structlog.get_logger(__name__)

router = APIRouter(tags=["webhook"])

# Events worth a review. Everything else is acknowledged and dropped, so ADO
# stops retrying it.
REVIEWABLE_EVENTS = {
    "git.pullrequest.created",
    "git.pullrequest.updated",
    "git.pullrequest.merged",
}


# --------------------------------------------------------------------- webhook
def verify_basic_auth(authorization: str | None) -> None:
    """Constant-time check of the service-hook Basic credentials.

    If no password is configured we refuse rather than accept: an unauthenticated
    ingress that triggers paid model calls is a denial-of-wallet vector, and
    failing closed at startup is easier to notice than a quiet open door.
    """
    expected_user = settings.ado_webhook_username
    expected_pass = settings.ado_webhook_password

    if not expected_pass:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Webhook ingress is not configured: set ADO_WEBHOOK_PASSWORD.",
        )
    if not authorization or not authorization.lower().startswith("basic "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Missing Basic credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    try:
        decoded = base64.b64decode(authorization.split(" ", 1)[1]).decode()
        user, _, password = decoded.partition(":")
    except Exception:  # noqa: BLE001
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Malformed Basic credentials") from None

    ok_user = hmac.compare_digest(user, expected_user)
    ok_pass = hmac.compare_digest(password, expected_pass)
    if not (ok_user and ok_pass):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")


def parse_event(body: dict[str, Any]) -> tuple[PullRequestRef, str, str, str]:
    """Pull a PullRequestRef out of a service-hook payload.

    Returns (pr, event_type, notification_id, idempotency_key).
    """
    event_type = body.get("eventType") or ""
    resource = body.get("resource") or {}
    repository = resource.get("repository") or {}

    if not resource.get("pullRequestId") or not repository.get("id"):
        # Literal 422 rather than the Starlette constant, whose name moved
        # between releases (UNPROCESSABLE_ENTITY -> UNPROCESSABLE_CONTENT).
        raise HTTPException(
            422, "Payload is missing resource.pullRequestId or resource.repository.id"
        )

    # Shared with the polling trigger, so both produce identical refs and
    # therefore identical idempotency keys.
    pr = pull_request_ref_from_resource(resource)

    notification_id = str(body.get("id") or body.get("notificationId") or "")
    idempotency_key = idempotency_key_for(pr, fallback=notification_id)
    return pr, event_type, notification_id, idempotency_key


@router.post("/webhooks/azure-devops", status_code=status.HTTP_202_ACCEPTED)
async def receive(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_basic_auth(authorization)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body is not valid JSON") from None

    pr, event_type, notification_id, idempotency_key = parse_event(body)

    if event_type not in REVIEWABLE_EVENTS:
        log.info("webhook.ignored", event_type=event_type, pr=pr.slug)
        return {"status": "ignored", "reason": f"event '{event_type}' is not reviewable"}

    review_id = await repo.claim_delivery(idempotency_key, notification_id, event_type)
    if review_id is None:
        existing = await repo.existing_review_for_key(idempotency_key)
        log.info("webhook.duplicate", pr=pr.slug, key=idempotency_key)
        return {
            "status": "duplicate",
            "review_id": str(existing) if existing else None,
            "idempotency_key": idempotency_key,
        }

    review_request = ReviewRequest(
        review_id=review_id,
        pr=pr,
        idempotency_key=idempotency_key,
        event_type=event_type,
    )
    await repo.create_review(review_request)

    events.bind_trace(events.new_trace_id(), review_id)
    await events.emit(
        "webhook.received",
        review_id=review_id,
        event_type_name=event_type,
        pr=pr.slug,
        notification_id=notification_id,
    )

    await enqueue_review(review_request)
    log.info("webhook.accepted", pr=pr.slug, review_id=str(review_id))
    return {"status": "accepted", "review_id": str(review_id), "pr": pr.slug}


@router.post("/webhooks/azure-devops/test", status_code=status.HTTP_200_OK)
async def test_credentials(authorization: str | None = Header(default=None)) -> dict[str, str]:
    """Lets you verify subscription credentials without creating a review."""
    verify_basic_auth(authorization)
    return {"status": "ok"}


# --------------------------------------------------------------------- polling
@dataclass
class PollResult:
    scanned: int = 0
    queued: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def poll_once(
    project: str,
    repository_id: str | None = None,
    *,
    client: AzureDevOpsClient | None = None,
    include_drafts: bool = False,
    dry_run: bool = False,
) -> PollResult:
    """One sweep: list active PRs, enqueue the ones not already claimed.

    `dry_run` reports what would be picked up without claiming or reviewing
    anything - worth running first against a real project, because the
    non-dry path posts comments to real pull requests.
    """
    result = PollResult()
    owned = client is None
    ado = client or get_ado_client()

    try:
        # Always fetch drafts so the board can show them; whether they are
        # *reviewed* is decided below. A pull request the dashboard cannot see
        # is one a reviewer assumes was missed.
        pull_requests = await ado.list_active_pull_requests(
            project, repository_id, include_drafts=True
        )
    except Exception as exc:  # noqa: BLE001
        log.error("poll.list.failed", project=project, error=str(exc)[:300])
        result.errors.append(str(exc))
        if owned:
            await ado.aclose()
        return result

    reviewable = [pr for pr in pull_requests if include_drafts or not pr.is_draft]
    result.scanned = len(reviewable)

    try:
        # Record everything, including drafts and anything already reviewed, so
        # the board reflects the whole project rather than only what this
        # system happened to act on. This runs during a dry run too: observing
        # what exists is not claiming, reviewing or posting, and a dry run that
        # left the board empty would be the less useful half of the feature.
        for pr in pull_requests:
            await repo.upsert_pull_request(pr)
        if repository_id is None:
            # Whole-project sweep, so anything that vanished has been merged
            # or abandoned.
            await repo.close_absent_pull_requests(
                project, [pr.pull_request_id for pr in pull_requests]
            )

        for pr in reviewable:
            if dry_run:
                already = await repo.existing_review_for_key(idempotency_key_for(pr))
                target = result.skipped if already or not pr.source_commit else result.queued
                target.append(pr.slug)
                continue
            queued = await _claim_and_enqueue(pr)
            (result.queued if queued else result.skipped).append(pr.slug)
    finally:
        if owned:
            await ado.aclose()

    log.info(
        "poll.complete",
        project=project,
        scanned=result.scanned,
        queued=len(result.queued),
        skipped=len(result.skipped),
    )
    return result


async def _claim_and_enqueue(pr: PullRequestRef) -> bool:
    """Claim this head commit, or report that someone already has it."""
    key = idempotency_key_for(pr)
    if not pr.source_commit:
        # No merge commit yet - Azure DevOps is still computing it. Skipping
        # means the next sweep picks it up with a stable key, rather than
        # claiming one now that a later sweep would not match.
        log.info("poll.pending_merge_commit", pr=pr.slug)
        return False

    review_id = await repo.claim_delivery(key, notification_id="", event_type="poll")
    if review_id is None:
        return False  # already reviewed, or claimed by the webhook

    request = ReviewRequest(
        review_id=review_id, pr=pr, idempotency_key=key, event_type="poll"
    )
    await repo.create_review(request)

    events.bind_trace(events.new_trace_id(), review_id)
    await events.emit(
        "poll.claimed", review_id=review_id, pr=pr.slug, commit=pr.source_commit[:12]
    )

    await enqueue_review(request)
    log.info("poll.queued", pr=pr.slug, review_id=str(review_id))
    return True


async def poll_project(
    project: str,
    repository_ids: list[str] | None = None,
    *,
    client: AzureDevOpsClient | None = None,
    include_drafts: bool = False,
    dry_run: bool = False,
) -> PollResult:
    """Sweep a project, optionally restricted to a set of repositories.

    `repository_ids` blank means the whole project - unchanged default
    behaviour, and what a webhook subscription or a one-off `/api/index` call
    still gets, since those go through `poll_once`/single-repo paths directly.
    Given a list (`ADO_REPOSITORIES`, or `--repo` passed more than once), this
    sweeps each repository in turn against a shared client and merges the
    results, so a repo not on the list is never listed, claimed, or reviewed.
    """
    if not repository_ids:
        return await poll_once(
            project, None, client=client, include_drafts=include_drafts, dry_run=dry_run
        )

    owned = client is None
    ado = client or get_ado_client()
    merged = PollResult()
    try:
        for repository_id in repository_ids:
            one = await poll_once(
                project, repository_id, client=ado, include_drafts=include_drafts, dry_run=dry_run
            )
            merged.scanned += one.scanned
            merged.queued.extend(one.queued)
            merged.skipped.extend(one.skipped)
            merged.errors.extend(one.errors)
    finally:
        if owned:
            await ado.aclose()
    return merged


async def poll_forever(
    project: str,
    repository_ids: list[str] | None = None,
    *,
    interval_seconds: int = 60,
    include_drafts: bool = False,
) -> None:
    """Sweep on an interval until cancelled.

    A failed sweep is logged and retried on the next tick rather than killing
    the loop: a transient Azure DevOps blip should not require a restart.
    """
    log.info(
        "poll.started",
        project=project,
        repository_ids=repository_ids or "*",
        interval_seconds=interval_seconds,
    )
    while True:
        try:
            await poll_project(
                project, repository_ids, include_drafts=include_drafts
            )
        except asyncio.CancelledError:
            log.info("poll.stopped")
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("poll.sweep.failed", error=str(exc)[:300])
        await asyncio.sleep(interval_seconds)


__all__ = [
    "router", "parse_event", "verify_basic_auth", "REVIEWABLE_EVENTS", "UUID",
    "PollResult", "poll_once", "poll_project", "poll_forever",
]


# --------------------------------------------------------------------- CLI
# `python -m app.contracts.triggers --project ...` - watches Azure DevOps for
# pull requests and reviews them, no public URL needed. The webhook path needs
# Azure DevOps to reach your machine; this inverts it: your machine asks Azure
# DevOps what is open. Shares the idempotency table with the webhook, so
# running both is safe. This is what the `poller` service in docker-compose.yml
# runs.
async def _cli_main() -> int:
    import argparse
    import sys

    from app.platform import db
    from app.platform.observability import configure_logging

    parser = argparse.ArgumentParser(description=_cli_main.__doc__)
    parser.add_argument("--project", default=settings.ado_project)
    parser.add_argument(
        "--repo",
        action="append",
        default=None,
        help="Repository id or name. Repeatable. Omit to use ADO_REPOSITORIES, "
        "or the whole project if that's blank too.",
    )
    parser.add_argument("--interval", type=int, default=60, help="Seconds between sweeps")
    parser.add_argument("--once", action="store_true", help="One sweep, then exit")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be reviewed without claiming or posting anything. "
        "Implies --once.",
    )
    parser.add_argument(
        "--include-drafts", action="store_true", help="Also review draft pull requests"
    )
    args = parser.parse_args()

    configure_logging()

    if not args.project:
        print("No project. Pass --project or set ADO_PROJECT.", file=sys.stderr)
        return 2
    if not settings.ado_pat:
        print("ADO_PAT is not set.", file=sys.stderr)
        return 2

    repository_ids = args.repo or settings.ado_repositories

    mode = "redis worker" if settings.queue_mode == "redis" else "inline (this process)"
    print(f"Watching {settings.ado_org_url}/{args.project}"
          f"{'/' + ','.join(repository_ids) if repository_ids else ''}")
    print(f"  reviews run: {mode}")
    if settings.queue_mode == "redis":
        print("  (start the worker too: arq app.service.queue.WorkerSettings)")
    print()

    await db.init_pool()
    try:
        await db.run_migrations()
        if args.once or args.dry_run:
            result = await poll_project(
                args.project,
                repository_ids,
                include_drafts=args.include_drafts,
                dry_run=args.dry_run,
            )
            print(f"scanned {result.scanned} active pull request(s)")
            verb = "would review" if args.dry_run else "queued "
            for slug in result.queued:
                print(f"  {verb} {slug}")
            for slug in result.skipped:
                print(f"  skipped {slug}  (already reviewed, or no merge commit yet)")
            if args.dry_run and result.queued:
                print("\nDry run - nothing claimed, nothing posted.")
            for error in result.errors:
                print(f"  error   {error}", file=sys.stderr)
            return 1 if result.errors else 0

        print("Polling. Ctrl-C to stop.\n")
        await poll_forever(
            args.project,
            repository_ids,
            interval_seconds=args.interval,
            include_drafts=args.include_drafts,
        )
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        await db.close_pool()
    return 0


if __name__ == "__main__":
    import asyncio

    try:
        raise SystemExit(asyncio.run(_cli_main()))
    except KeyboardInterrupt:
        raise SystemExit(0) from None
