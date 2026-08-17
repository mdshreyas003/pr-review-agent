"""The workflow engine, and the review flow built on it.

    prepare -> fan_out -> aggregate -> gate -> publish

The rest of the system describes a review as an ordered list of named nodes and
never mentions how they are executed. That indirection is the whole point: the
built-in engine below is a few dozen lines of asyncio, and it is the right size
for this workload today - a five-way fan-out with a checkpoint between phases.
When concurrency grows past what a single worker's event loop should own, a
Temporal or LangGraph engine implements this same protocol and nothing below
`build_nodes` changes.

Checkpointing exists so a worker that dies mid-review does not restart from the
webhook. Resuming skips completed nodes and replays the rest.

Everything expensive happens inside `fan_out`, which runs the five specialists
concurrently. The nodes on either side are cheap and exist mostly so there is
somewhere honest to put a checkpoint and a span boundary. `prepare` is the only
node that can abort the review outright (nothing to review, or the diff could
not be fetched). Every later node degrades instead: a failed aggregator still
yields findings, a failed publish still yields a persisted review a human can
read in the dashboard. Every node is also checked for cancellation before it
runs (see `_guarded`), so a task cancelled while queued or mid-review stops
between steps rather than running to completion.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

import structlog

from app.agent.aggregator import merge_findings, summarise
from app.agent.hitl import GateDecision
from app.agent.hitl import decide as gate_decide
from app.agent.hitl import enqueue_for_human
from app.agent.specialists import ReviewContext, build_specialists
from app.config import settings
from app.contracts.azure import AzureDevOpsClient
from app.contracts.models import AgentResult, ReviewOutcome, ReviewRequest
from app.platform import observability as events
from app.platform import repositories as repo

log = structlog.get_logger(__name__)

NodeFn = Callable[["WorkflowState"], Awaitable[None]]


# ------------------------------------------------------------------ workflow
@dataclass
class WorkflowState:
    """Mutable state threaded through the nodes.

    `data` is the durable part - anything a resumed run needs must be
    JSON-serialisable and live here. `scratch` holds live objects (clients,
    parsed models) that a resumed run rebuilds instead of restoring.
    """

    workflow_id: str
    data: dict[str, Any] = field(default_factory=dict)
    scratch: dict[str, Any] = field(default_factory=dict)
    completed: list[str] = field(default_factory=list)

    def to_checkpoint(self) -> str:
        return json.dumps({"data": self.data, "completed": self.completed}, default=str)

    @classmethod
    def from_checkpoint(cls, workflow_id: str, blob: str) -> WorkflowState:
        payload = json.loads(blob)
        return cls(
            workflow_id=workflow_id,
            data=payload.get("data", {}),
            completed=payload.get("completed", []),
        )


@dataclass(slots=True)
class Node:
    name: str
    fn: NodeFn
    # A node that fails without stopping the review - the docs specialist going
    # down should not cost you the security findings.
    optional: bool = False


class Checkpointer(Protocol):
    async def save(self, workflow_id: str, blob: str) -> None: ...
    async def load(self, workflow_id: str) -> str | None: ...
    async def clear(self, workflow_id: str) -> None: ...


class NullCheckpointer:
    """No durability. Correct for tests and for single-shot local runs."""

    async def save(self, workflow_id: str, blob: str) -> None:
        return None

    async def load(self, workflow_id: str) -> str | None:
        return None

    async def clear(self, workflow_id: str) -> None:
        return None


class RedisCheckpointer:
    """State survives a worker restart for as long as the TTL."""

    def __init__(self, redis_url: str, ttl_seconds: int = 86_400) -> None:
        self._url = redis_url
        self._ttl = ttl_seconds
        self._client: Any = None

    async def _conn(self) -> Any:
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(self._url, decode_responses=True)
        return self._client

    def _key(self, workflow_id: str) -> str:
        return f"prreview:checkpoint:{workflow_id}"

    async def save(self, workflow_id: str, blob: str) -> None:
        try:
            client = await self._conn()
            await client.set(self._key(workflow_id), blob, ex=self._ttl)
        except Exception as exc:  # noqa: BLE001 - a lost checkpoint is not fatal
            log.warning(
                "checkpoint.save.failed", workflow_id=workflow_id, error=str(exc)
            )

    async def load(self, workflow_id: str) -> str | None:
        try:
            client = await self._conn()
            return await client.get(self._key(workflow_id))
        except Exception as exc:  # noqa: BLE001
            log.warning("checkpoint.load.failed", workflow_id=workflow_id, error=str(exc))
            return None

    async def clear(self, workflow_id: str) -> None:
        try:
            client = await self._conn()
            await client.delete(self._key(workflow_id))
        except Exception as exc:  # noqa: BLE001
            log.warning("checkpoint.clear.failed", workflow_id=workflow_id, error=str(exc))


class WorkflowEngine(Protocol):
    async def run(
        self,
        workflow_id: str,
        nodes: list[Node],
        initial: dict[str, Any],
        scratch: dict[str, Any] | None = None,
    ) -> WorkflowState:
        ...


class NativeWorkflowEngine:
    """Sequential nodes, checkpointed between each. Fan-out lives inside a node."""

    def __init__(self, checkpointer: Checkpointer | None = None) -> None:
        self.checkpointer = checkpointer or NullCheckpointer()

    async def run(
        self,
        workflow_id: str,
        nodes: list[Node],
        initial: dict[str, Any],
        scratch: dict[str, Any] | None = None,
    ) -> WorkflowState:
        blob = await self.checkpointer.load(workflow_id)
        if blob:
            state = WorkflowState.from_checkpoint(workflow_id, blob)
            state.data.update(initial)
            log.info("workflow.resumed", workflow_id=workflow_id, completed=state.completed)
        else:
            state = WorkflowState(workflow_id=workflow_id, data=dict(initial))
        state.scratch.update(scratch or {})

        for node in nodes:
            if node.name in state.completed:
                log.debug("workflow.node.skipped", node=node.name, workflow_id=workflow_id)
                continue
            try:
                await node.fn(state)
            except Exception as exc:  # noqa: BLE001
                if not node.optional:
                    await self.checkpointer.save(workflow_id, state.to_checkpoint())
                    raise
                log.warning(
                    "workflow.node.optional_failed",
                    node=node.name,
                    workflow_id=workflow_id,
                    error=str(exc)[:300],
                )
            state.completed.append(node.name)
            await self.checkpointer.save(workflow_id, state.to_checkpoint())

        await self.checkpointer.clear(workflow_id)
        return state


def build_engine() -> NativeWorkflowEngine:
    if settings.queue_mode == "inline":
        return NativeWorkflowEngine(NullCheckpointer())
    return NativeWorkflowEngine(RedisCheckpointer(settings.redis_url))


# --------------------------------------------------------------------- review
class ReviewAborted(Exception):
    """Nothing reviewable. Carries the terminal status to record."""

    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class ReviewCancelled(Exception):
    """The task was cancelled; caught between graph steps, not mid-model-call."""


def _guarded(fn: NodeFn, review_id: UUID) -> NodeFn:
    async def wrapped(state: WorkflowState) -> None:
        if await repo.get_review_status(review_id) == "cancelled":
            raise ReviewCancelled()
        await fn(state)

    return wrapped


# --------------------------------------------------------------------- nodes
async def prepare(state: WorkflowState) -> None:
    request: ReviewRequest = state.scratch["request"]
    client: AzureDevOpsClient = state.scratch["ado"]
    pr = request.pr

    async with events.span("prepare", review_id=request.review_id, pr=pr.slug) as span:
        diffs = await client.get_diff(pr)
        story = await client.get_story_context(pr)
        context = ReviewContext(pr, diffs, story)
        span.update(
            files=len(diffs),
            work_items=len(story.work_items),
            delivery_plan=story.delivery_plan_name or None,
        )

        if context.is_empty:
            raise ReviewAborted(
                "skipped", "no textual changes to review (binary, empty, or deleted-only diff)"
            )

    state.scratch["context"] = context
    state.data["files"] = [d.path for d in diffs]
    state.data["work_item_ids"] = [w.id for w in story.work_items]


async def fan_out(state: WorkflowState) -> None:
    """Run the specialists concurrently; never let one failure cancel the others."""
    context: ReviewContext = state.scratch["context"]
    request: ReviewRequest = state.scratch["request"]
    specialists = build_specialists(settings.enabled_agents)

    async with events.span(
        "fan_out", review_id=request.review_id, specialists=len(specialists)
    ):
        # return_exceptions is redundant given Specialist.run never raises, but
        # it means a genuine bug in one agent cannot take down the review.
        results: list[Any] = await asyncio.gather(
            *(s.run(context) for s in specialists), return_exceptions=True
        )

    agent_results: list[AgentResult] = []
    for specialist, result in zip(specialists, results, strict=True):
        if isinstance(result, BaseException):
            log.error(
                "fan_out.agent.crashed",
                agent=specialist.agent_type,
                error=str(result),
                exc_info=result,
            )
            agent_results.append(
                AgentResult(
                    agent_type=specialist.agent_type,  # type: ignore[arg-type]
                    error=f"{type(result).__name__}: {result}",
                    degraded=True,
                )
            )
        else:
            agent_results.append(result)

    state.scratch["agent_results"] = agent_results
    state.data["agent_summary"] = [
        {"agent": r.agent_type, "findings": len(r.findings), "degraded": r.degraded}
        for r in agent_results
    ]


async def aggregate(state: WorkflowState) -> None:
    request: ReviewRequest = state.scratch["request"]
    context: ReviewContext = state.scratch["context"]
    agent_results: list[AgentResult] = state.scratch["agent_results"]

    async with events.span("aggregate", review_id=request.review_id) as span:
        findings = merge_findings(agent_results)
        summary, confidence, aggregator_usage = await summarise(
            context.pr, findings, agent_results
        )
        span.update(
            findings=len(findings),
            confidence=confidence,
            input_tokens=aggregator_usage.input_tokens,
            output_tokens=aggregator_usage.output_tokens,
        )

    outcome = ReviewOutcome(
        review_id=request.review_id,
        pr=context.pr,
        status="running",
        findings=findings,
        agent_results=agent_results,
        overall_confidence=confidence,
        summary=summary,
    )
    state.scratch["outcome"] = outcome
    state.data["findings"] = len(findings)
    state.data["confidence"] = confidence


async def apply_gate(state: WorkflowState) -> None:
    outcome: ReviewOutcome = state.scratch["outcome"]
    degraded = sum(1 for r in outcome.agent_results if not r.ok)
    decision = gate_decide(outcome.findings, outcome.overall_confidence, degraded)

    outcome.requires_human = decision.requires_human
    outcome.escalated = decision.escalate
    state.scratch["decision"] = decision
    state.data["auto_post"] = decision.auto_post
    state.data["gate_reason"] = decision.reason

    await events.emit(
        "gate.decided",
        review_id=outcome.review_id,
        auto_post=decision.auto_post,
        escalate=decision.escalate,
        reason=decision.reason,
        confidence=outcome.overall_confidence,
        degraded_agents=degraded,
    )


async def publish_or_queue(state: WorkflowState) -> None:
    outcome: ReviewOutcome = state.scratch["outcome"]
    decision: GateDecision = state.scratch["decision"]
    client: AzureDevOpsClient = state.scratch["ado"]

    # Defence in depth. The gate already refuses to auto-post when human
    # approval is required, so reaching here with auto_post set would mean a
    # bug in the gate - and the cost of that bug is comments on other people's
    # pull requests, which cannot be taken back. Cheap check, severe failure.
    if settings.require_human_approval and decision.auto_post:
        log.error(
            "publish.blocked",
            review_id=str(outcome.review_id),
            reason="auto_post was set while REQUIRE_HUMAN_APPROVAL is on",
        )
        await events.emit(
            "publish.blocked",
            review_id=outcome.review_id,
            status="error",
            reason="gate returned auto_post while human approval is required",
        )
        decision = GateDecision(
            auto_post=False,
            escalate=decision.escalate,
            reason="blocked: human approval is required for every review",
        )
        state.scratch["decision"] = decision
        outcome.requires_human = True

    if not decision.auto_post:
        outcome.status = "awaiting_approval"
        await repo.save_outcome(outcome)
        await enqueue_for_human(outcome, decision)
        return

    from app.agent.runner import publish

    await repo.save_outcome(outcome)  # persist before posting, so a failed
    await publish(client, outcome)     # post never loses the findings
    outcome.status = "posted"
    await repo.set_review_status(outcome.review_id, "posted")


def build_nodes() -> list[Node]:
    return [
        Node("prepare", prepare),
        Node("fan_out", fan_out),
        Node("aggregate", aggregate),
        Node("gate", apply_gate),
        Node("publish", publish_or_queue),
    ]


# ------------------------------------------------------------------ entrypoint
async def execute(request: ReviewRequest, client: AzureDevOpsClient) -> ReviewOutcome:
    started = time.perf_counter()
    engine = build_engine()
    nodes = [
        Node(n.name, _guarded(n.fn, request.review_id), optional=n.optional)
        for n in build_nodes()
    ]

    try:
        state = await engine.run(
            workflow_id=str(request.review_id),
            nodes=nodes,
            initial={"pr": request.pr.slug, "event_type": request.event_type},
            scratch={"request": request, "ado": client},
        )
    except ReviewCancelled:
        log.info("review.cancelled", pr=request.pr.slug, review_id=str(request.review_id))
        await events.emit(
            "review.cancelled", review_id=request.review_id, terminal_status="cancelled"
        )
        outcome = ReviewOutcome(
            review_id=request.review_id,
            pr=request.pr,
            status="cancelled",
            summary="cancelled by request",
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        await repo.save_outcome(outcome)
        return outcome
    except ReviewAborted as abort:
        log.info("review.aborted", pr=request.pr.slug, status=abort.status, reason=abort.reason)
        await events.emit(
            "review.aborted",
            review_id=request.review_id,
            terminal_status=abort.status,
            reason=abort.reason,
        )
        outcome = ReviewOutcome(
            review_id=request.review_id,
            pr=request.pr,
            status=abort.status,  # type: ignore[arg-type]
            summary=abort.reason,
            error=abort.reason if abort.status != "skipped" else None,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        await repo.save_outcome(outcome)
        return outcome

    outcome: ReviewOutcome = state.scratch["outcome"]
    outcome.duration_ms = int((time.perf_counter() - started) * 1000)
    await repo.save_outcome(outcome)
    return outcome
