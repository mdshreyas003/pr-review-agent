"""The contracts every module agrees on.

`Finding` is the output contract of the whole system. Agents produce it,
the aggregator merges it, the HITL gate routes on it, and the Azure DevOps
client renders it into a PR comment thread. Nothing downstream is allowed to
invent a field that isn't here.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"

    @property
    def rank(self) -> int:
        return {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "INFO": 0}[self.value]


AgentType = Literal["security", "quality", "tests", "docs", "story"]

ReviewStatus = Literal[
    "queued",
    "running",
    "awaiting_approval",
    "posted",
    "rejected",
    "failed",
    "skipped",
    "cancelled",
]

HitlDecision = Literal["pending", "approved", "rejected", "edited", "expired"]


class Finding(BaseModel):
    """One reviewable observation, attributable to exactly one agent."""

    model_config = ConfigDict(use_enum_values=False)

    id: UUID = Field(default_factory=uuid4)
    agent_type: AgentType
    severity: Severity
    category: str = Field(max_length=64)
    file_path: str
    line_start: int = Field(ge=0)
    line_end: int = Field(ge=0)
    title: str = Field(max_length=200)
    rationale: str
    suggestion: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    # Chunk ids from the retrieval step that grounded this finding. An
    # ungrounded finding is not necessarily wrong, but it is cheaper to doubt.
    citations: list[str] = Field(default_factory=list)
    merged_from: list[str] = Field(default_factory=list)

    @field_validator("line_end")
    @classmethod
    def _end_after_start(cls, v: int, info) -> int:
        start = info.data.get("line_start", 0)
        return max(v, start)

    @property
    def dedupe_key(self) -> str:
        """Two agents flagging the same lines for the same reason are one finding.

        Keyed on location + category rather than prose, because the prose is
        exactly the part that differs between agents.
        """
        raw = f"{self.file_path}:{self.line_start}:{self.category.lower()}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def to_markdown(self) -> str:
        icon = {
            Severity.CRITICAL: "🛑",
            Severity.HIGH: "⚠️",
            Severity.MEDIUM: "🔸",
            Severity.LOW: "🔹",
            Severity.INFO: "ℹ️",
        }[self.severity]
        parts = [
            f"{icon} **{self.severity.value} · {self.category}** — {self.title}",
            "",
            self.rationale.strip(),
        ]
        if self.suggestion:
            parts += ["", "**Suggested fix**", "", "```suggestion", self.suggestion.strip(), "```"]
        agents = ", ".join(sorted({self.agent_type, *self.merged_from}))
        parts += ["", f"<sub>agent: {agents} · confidence: {self.confidence:.2f}</sub>"]
        return "\n".join(parts)


class PullRequestRef(BaseModel):
    """Everything needed to fetch and comment on a PR, and nothing more."""

    organization_url: str
    project: str
    repository_id: str
    repository_name: str
    pull_request_id: int
    title: str = ""
    description: str = ""
    source_branch: str = ""
    target_branch: str = ""
    author: str = ""
    source_commit: str = ""
    target_commit: str = ""
    is_draft: bool = False

    @property
    def slug(self) -> str:
        return f"{self.project}/{self.repository_name}#{self.pull_request_id}"

    @property
    def web_url(self) -> str:
        """Link a reviewer can actually click, back to Azure DevOps."""
        if not (self.organization_url and self.project and self.repository_name):
            return ""
        return (
            f"{self.organization_url}/{self.project}/_git/"
            f"{self.repository_name}/pullrequest/{self.pull_request_id}"
        )


class DiffHunk(BaseModel):
    line_start: int
    line_end: int
    content: str


class FileDiff(BaseModel):
    path: str
    change_type: Literal["add", "edit", "delete", "rename"] = "edit"
    hunks: list[DiffHunk] = Field(default_factory=list)
    added_lines: int = 0
    removed_lines: int = 0
    is_binary: bool = False
    truncated: bool = False

    def render(self, max_chars: int = 24_000) -> str:
        body = "\n".join(
            f"@@ lines {h.line_start}-{h.line_end} @@\n{h.content}" for h in self.hunks
        )
        if len(body) > max_chars:
            body = body[:max_chars] + "\n... [truncated]"
        header = (
            f"### {self.path} ({self.change_type}, "
            f"+{self.added_lines}/-{self.removed_lines})"
        )
        return f"{header}\n{body}"


class WorkItem(BaseModel):
    """An Azure Boards work item linked to the PR."""

    id: int
    title: str = ""
    work_item_type: str = ""
    state: str = ""
    description: str = ""
    acceptance_criteria: str = ""
    board_column: str = ""
    parent_id: int | None = None
    parent_title: str = ""
    parent_type: str = ""
    url: str = ""

    def render(self) -> str:
        lines = [f"#{self.id} [{self.work_item_type}] {self.title} (state: {self.state})"]
        if self.parent_id:
            lines.append(f"  parent: #{self.parent_id} [{self.parent_type}] {self.parent_title}")
        if self.board_column:
            lines.append(f"  board column: {self.board_column}")
        if self.description:
            lines.append(f"  description: {self.description.strip()[:1500]}")
        if self.acceptance_criteria:
            lines.append(f"  acceptance criteria:\n{self.acceptance_criteria.strip()[:2500]}")
        return "\n".join(lines)


class StoryContext(BaseModel):
    """Story-mapping context pulled from Azure Boards + Delivery Plans."""

    work_items: list[WorkItem] = Field(default_factory=list)
    delivery_plan_name: str = ""
    delivery_plan_teams: list[str] = Field(default_factory=list)
    iteration_path: str = ""

    @property
    def has_context(self) -> bool:
        return bool(self.work_items)

    def render(self) -> str:
        if not self.work_items:
            return "No linked work items. The PR is not traceable to a user story."
        head = []
        if self.delivery_plan_name:
            head.append(f"Delivery plan: {self.delivery_plan_name}")
        if self.iteration_path:
            head.append(f"Iteration: {self.iteration_path}")
        return "\n".join(head + [wi.render() for wi in self.work_items])


class RetrievedChunk(BaseModel):
    chunk_id: str
    file_path: str
    line_start: int
    line_end: int
    content: str
    score: float = 0.0
    source: Literal["vector", "keyword", "hybrid", "mem0"] = "hybrid"

    def render(self) -> str:
        header = f"--- {self.file_path}:{self.line_start}-{self.line_end} (id={self.chunk_id})"
        return f"{header}\n{self.content}"


class ReviewRequest(BaseModel):
    """The unit of work handed to the orchestrator."""

    review_id: UUID = Field(default_factory=uuid4)
    pr: PullRequestRef
    idempotency_key: str
    event_type: str = "git.pullrequest.created"
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentResult(BaseModel):
    """One specialist's contribution, including how it failed if it did."""

    agent_type: AgentType
    findings: list[Finding] = Field(default_factory=list)
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    degraded: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class ReviewOutcome(BaseModel):
    review_id: UUID
    pr: PullRequestRef
    status: ReviewStatus
    findings: list[Finding] = Field(default_factory=list)
    agent_results: list[AgentResult] = Field(default_factory=list)
    overall_confidence: float = 0.0
    requires_human: bool = False
    escalated: bool = False
    summary: str = ""
    duration_ms: int = 0
    error: str | None = None
    posted_thread_ids: list[int] = Field(default_factory=list)

    @property
    def max_severity(self) -> Severity | None:
        return max((f.severity for f in self.findings), key=lambda s: s.rank, default=None)


class AgentEvent(BaseModel):
    """One row on the time-ordered spine. Immutable by construction."""

    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    review_id: UUID | None = None
    trace_id: str = ""
    span_id: str = ""
    parent_span_id: str = ""
    agent_type: str = ""
    event_type: str
    status: str = "ok"
    duration_ms: int = 0
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)
