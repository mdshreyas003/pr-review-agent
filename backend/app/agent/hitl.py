"""The autonomy gate.

The system runs at "human handles exceptions": most reviews post themselves,
and the ones a human sees are the ones where automation would be a bad bet.
Three rules decide, in this order:

  any CRITICAL      -> escalate; a human sees it before the author does
  low confidence    -> queue for approval
  otherwise         -> post

The ordering matters. A CRITICAL finding at high confidence is *more* worth a
human's eyes, not less - confidence is a measure of how sure we are, not of how
little it matters.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import structlog

from app.config import settings
from app.contracts.models import Finding, ReviewOutcome, Severity
from app.platform import observability as events
from app.platform import repositories as repo

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class GateDecision:
    auto_post: bool
    escalate: bool
    reason: str

    @property
    def requires_human(self) -> bool:
        return not self.auto_post


def decide(findings: list[Finding], confidence: float, degraded_agents: int = 0) -> GateDecision:
    if settings.require_human_approval:
        # Checked first and unconditionally. Not a threshold that a confident
        # review can clear - there is no score that posts without a human.
        return GateDecision(
            auto_post=False,
            escalate=any(f.severity is Severity.CRITICAL for f in findings),
            reason="human approval is required for every review (REQUIRE_HUMAN_APPROVAL)",
        )

    critical = [f for f in findings if f.severity is Severity.CRITICAL]
    if critical and settings.escalate_on_critical:
        return GateDecision(
            auto_post=False,
            escalate=True,
            reason=(
                f"{len(critical)} CRITICAL finding(s) - escalated for human review before posting"
            ),
        )

    if confidence < settings.auto_post_min_confidence:
        return GateDecision(
            auto_post=False,
            escalate=False,
            reason=(
                f"overall confidence {confidence:.2f} is below the "
                f"{settings.auto_post_min_confidence:.2f} auto-post threshold"
            ),
        )

    if degraded_agents >= 3:
        # Three of five specialists failing is a coverage problem, not a review.
        return GateDecision(
            auto_post=False,
            escalate=False,
            reason=f"{degraded_agents} specialists failed - coverage too thin to post unreviewed",
        )

    return GateDecision(
        auto_post=True,
        escalate=False,
        reason=f"confidence {confidence:.2f}, no critical findings",
    )


async def enqueue_for_human(outcome: ReviewOutcome, decision: GateDecision) -> UUID:
    hitl_id = await repo.create_hitl(outcome.review_id, decision.reason)
    await events.emit(
        "hitl.queued",
        review_id=outcome.review_id,
        hitl_id=str(hitl_id),
        escalated=decision.escalate,
        reason=decision.reason,
        confidence=outcome.overall_confidence,
        findings=len(outcome.findings),
    )
    log.info(
        "hitl.queued",
        review_id=str(outcome.review_id),
        escalated=decision.escalate,
        reason=decision.reason,
    )
    return hitl_id
