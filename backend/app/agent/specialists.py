"""The shared review context, the specialist base class, and the five agents.

A specialist is: build a grounded prompt, call its routed model, translate the
result into `Finding` objects, and emit one span describing what that cost. The
only thing subclasses vary is whether they retrieve at all and whether they
need story context - everything else being identical is what makes the
fan-out uniform and the failure behaviour predictable.

Retrieval is agent-controlled, not a fixed pre-fetch: retrieval-eligible
specialists get `SEARCH_TOOL` and decide for themselves, mid-reasoning,
whether to call it, how many times (up to `settings.retrieval_max_tool_calls`),
and with what query - see `_search_tool` and `AzureFoundryLLM._structured_with_tools`.
A specialist that finds the diff self-contained can submit findings without
searching at all; the old behaviour of always running one fixed diff-derived
query before the model ever saw the diff is gone.

A specialist never raises into the orchestrator. A degraded `AgentResult` with
`error` set is the failure mode, because four specialists and one apology is a
better review than no review.

Five narrow reviewers rather than one broad one, because a single prompt asked
to weigh injection risk and docstring coverage in the same breath does neither
well, and because a per-agent failure is then survivable - a timed-out docs
agent costs you docs findings, not the review. Each class is thin on purpose:
the differences that matter are in `app.agent.prompts` and the model routing
table in `app.config`, both of which can change without code.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from app.agent.llm import cacheable, effort_for, get_llm, model_for
from app.agent.prompts import PROMPT_VERSION, SHARED_PREAMBLE, specialist_instructions
from app.config import settings
from app.contracts.llm_protocol import LLMClient, LLMError, ToolSpec
from app.contracts.models import (
    AgentResult,
    FileDiff,
    Finding,
    PullRequestRef,
    RetrievedChunk,
    Severity,
    StoryContext,
)
from app.contracts.schemas import RawFinding, SpecialistOutput
from app.platform import observability as events
from app.platform.memory import hybrid_search

log = structlog.get_logger(__name__)

# The one tool retrieval-eligible specialists get. The model decides itself
# whether, how many times, and with what query to call it - `hybrid_search`
# is no longer pre-fetched before the model ever runs (see `_search_tool`).
SEARCH_TOOL = ToolSpec(
    name="search_repository",
    description=(
        "Search the rest of the repository (not the diff, which you already "
        "have) for related code - existing callers, related functions, "
        "established patterns, prior handling of the same case. Returns the "
        "best-matching chunks, each tagged with its file, line range, and "
        "chunk id for citation. Call it with as many different queries as you "
        "need, or not at all if the diff is self-contained - you are not "
        "scored on how much you search."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "What to look for: an identifier, a behaviour, or a short "
                    "natural-language description of what you want to check."
                ),
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
)


class ReviewContext:
    """Everything gathered once and shared by all five specialists.

    Built before the fan-out so the diff is fetched once, not five times, and
    so the rendered PR block is byte-identical across agents - which is the
    precondition for the prompt cache doing anything.
    """

    def __init__(
        self,
        pr: PullRequestRef,
        diffs: list[FileDiff],
        story: StoryContext | None = None,
    ) -> None:
        self.pr = pr
        self.diffs = diffs
        self.story = story or StoryContext()
        self._rendered: str | None = None

    @property
    def changed_paths(self) -> set[str]:
        return {d.path for d in self.diffs}

    @property
    def is_empty(self) -> bool:
        return not any(d.hunks for d in self.diffs)

    def render_pr_block(self) -> str:
        """The shared, cacheable prefix. Must not vary between agents."""
        if self._rendered is None:
            files = "\n\n".join(d.render() for d in self.diffs if d.hunks) or "(no textual changes)"
            skipped = [d.path for d in self.diffs if d.is_binary or d.truncated]
            parts = [
                "## Pull request",
                f"Repository: {self.pr.project}/{self.pr.repository_name}",
                f"PR !{self.pr.pull_request_id}: {self.pr.title}",
                f"Author: {self.pr.author}",
                f"Branch: {self.pr.source_branch} -> {self.pr.target_branch}",
            ]
            if self.pr.description.strip():
                parts += ["", "### Description", self.pr.description.strip()[:4000]]
            parts += ["", "## Diff", files]
            if skipped:
                parts += ["", f"Not shown (binary or oversized): {', '.join(skipped)}"]
            self._rendered = "\n".join(parts)
        return self._rendered


class Specialist:
    agent_type: str = ""
    needs_story_context: bool = False
    uses_retrieval: bool = True

    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm

    @property
    def llm(self) -> LLMClient:
        return self._llm or get_llm()

    async def run(self, context: ReviewContext) -> AgentResult:
        started = time.perf_counter()
        model = model_for(self.agent_type)
        result = AgentResult(agent_type=self.agent_type, model=model)  # type: ignore[arg-type]
        # Populated by the search tool as the model calls it, not pre-fetched -
        # see `_search_tool`. Stays empty for specialists that don't retrieve.
        retrieved: list[RetrievedChunk] = []

        async with events.span(
            "agent.run",
            agent_type=self.agent_type,
            prompt_version=PROMPT_VERSION,
            pr=context.pr.slug,
        ) as span:
            span["model"] = model
            try:
                response = await asyncio.wait_for(
                    self._invoke(context, retrieved, model),
                    timeout=settings.agent_timeout_seconds,
                )
            except TimeoutError:
                result.error = f"timed out after {settings.agent_timeout_seconds}s"
                result.degraded = True
            except LLMError as exc:
                result.error = str(exc)
                result.degraded = True
            except Exception as exc:  # noqa: BLE001
                result.error = f"{type(exc).__name__}: {exc}"
                result.degraded = True
            else:
                result.findings = self._to_findings(response.parsed, context)
                usage = response.usage
                result.input_tokens = usage.input_tokens
                result.output_tokens = usage.output_tokens
                span.update(
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    findings=len(result.findings),
                    grounded_chunks=len(retrieved),
                )

            if result.error:
                # Recorded on the span, not raised: the span stays 'ok' because
                # the *system* handled this correctly. `degraded` is the signal.
                span["error"] = result.error
                span["degraded"] = True
                log.warning(
                    "agent.degraded", agent=self.agent_type, error=result.error, pr=context.pr.slug
                )

        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result

    # ------------------------------------------------------------- internals
    def _search_tool(self, context: ReviewContext, retrieved: list[RetrievedChunk]):
        """Build the tool executor the LLM client calls each time the model
        invokes `search_repository`. Closes over `retrieved` so `run()` can
        report how much grounding actually got used, whether the model called
        it zero times, once, or up to `retrieval_max_tool_calls` times."""

        async def _execute(name: str, args: dict[str, Any]) -> str:
            if name != SEARCH_TOOL.name:
                return f"Unknown tool '{name}'."
            query = str(args.get("query") or "").strip()
            if not query:
                return "search_repository needs a non-empty 'query'."
            chunks = await hybrid_search(
                context.pr.repository_id, query, exclude_paths=context.changed_paths
            )
            retrieved.extend(chunks)
            if not chunks:
                return "No matching repository chunks found for that query."
            return "\n\n".join(c.render() for c in chunks)

        return _execute

    async def _invoke(
        self, context: ReviewContext, retrieved: list[RetrievedChunk], model: str
    ):
        system = [cacheable(SHARED_PREAMBLE)]

        # Order matters: shared blocks first (identical bytes across all five
        # agents, so they cache), agent-specific instruction last.
        user: list[dict[str, Any]] = [cacheable(context.render_pr_block())]

        if self.needs_story_context:
            user.append(
                {
                    "type": "text",
                    "text": "## Linked work items (Azure Boards)\n" + context.story.render(),
                }
            )
        if self.uses_retrieval:
            user.append(
                {
                    "type": "text",
                    "text": (
                        "## Repository context\n"
                        "You have a `search_repository` tool to look up code elsewhere "
                        "in the repository, beyond what's shown in the diff above. Use "
                        "it to check an assumption before reporting a finding - existing "
                        "callers, how similar cases are handled elsewhere, whether input "
                        "is already validated upstream. Skip it if the diff is "
                        "self-contained. Cite the chunk ids you actually relied on."
                    ),
                }
            )
        user.append({"type": "text", "text": specialist_instructions(self.agent_type)})

        return await self.llm.structured(
            model=model,
            system=system,
            user=user,
            schema=SpecialistOutput,
            max_tokens=settings.max_tokens_per_agent,
            effort=effort_for(self.agent_type),
            tools=[SEARCH_TOOL] if self.uses_retrieval else None,
            tool_executor=self._search_tool(context, retrieved) if self.uses_retrieval else None,
            max_tool_rounds=settings.retrieval_max_tool_calls,
        )

    def _to_findings(self, output: SpecialistOutput, context: ReviewContext) -> list[Finding]:
        valid_paths = context.changed_paths
        findings: list[Finding] = []
        for raw in output.findings:
            finding = self._translate(raw, valid_paths)
            if finding is not None:
                findings.append(finding)
        return findings

    def _translate(self, raw: RawFinding, valid_paths: set[str]) -> Finding | None:
        """Clamp and sanity-check one model-produced finding.

        Anything the model can get wrong that would mislead a human - a path
        that isn't in the diff, a confidence of 7.0, a line number of -3 - is
        corrected or dropped here rather than shipped to a pull request.
        """
        confidence = min(max(float(raw.confidence), 0.0), 1.0)
        if confidence < settings.min_finding_confidence:
            return None

        path = raw.file_path.strip()
        if path and path not in valid_paths:
            # Models occasionally drop or add a leading slash relative to the diff.
            match = next(
                (p for p in valid_paths if p.lstrip("/") == path.lstrip("/")), None
            )
            if match:
                path = match
            else:
                log.info(
                    "agent.finding.unanchored",
                    agent=self.agent_type,
                    claimed_path=path,
                )
                path, raw.line_start, raw.line_end = "", 0, 0

        line_start = max(int(raw.line_start), 0)
        line_end = max(int(raw.line_end), line_start)

        return Finding(
            agent_type=self.agent_type,  # type: ignore[arg-type]
            severity=Severity(raw.severity),
            category=(raw.category.strip() or "general")[:64],
            file_path=path,
            line_start=line_start,
            line_end=line_end,
            title=raw.title.strip()[:200],
            rationale=raw.rationale.strip(),
            suggestion=raw.suggestion.strip() or None,
            confidence=confidence,
            citations=[c for c in raw.citations if c][:10],
        )


class SecurityAgent(Specialist):
    agent_type = "security"


class QualityAgent(Specialist):
    agent_type = "quality"


class TestsAgent(Specialist):
    agent_type = "tests"


class DocsAgent(Specialist):
    agent_type = "docs"
    # Docs findings come from the diff's own surface. Retrieved chunks mostly
    # tempt it into commenting on unchanged files.
    uses_retrieval = False


class StoryAgent(Specialist):
    """Checks the change against the Azure Boards work items it claims to implement."""

    agent_type = "story"
    needs_story_context = True
    uses_retrieval = False


ALL_SPECIALISTS: tuple[type[Specialist], ...] = (
    SecurityAgent,
    QualityAgent,
    TestsAgent,
    DocsAgent,
    StoryAgent,
)


def build_specialists(enabled: set[str] | None = None) -> list[Specialist]:
    return [cls() for cls in ALL_SPECIALISTS if enabled is None or cls.agent_type in enabled]
