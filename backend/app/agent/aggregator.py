"""Merging five opinions into one review.

The hard part is not combining findings, it is the near-duplicate: security and
quality both flag the same unvalidated parameter, in slightly different words,
on the same line. Posting both makes the review look careless. Merging on
location plus category - not on prose, which is exactly the part that differs -
collapses them into one finding that carries both agents' names and the higher
confidence of the two.
"""

from __future__ import annotations

import structlog

from app.agent.llm import get_llm
from app.agent.prompts import AGGREGATOR_INSTRUCTIONS
from app.config import settings
from app.contracts.llm_protocol import LLMClient, LLMError
from app.contracts.models import AgentResult, Finding, PullRequestRef, Severity
from app.contracts.schemas import AggregatorOutput

log = structlog.get_logger(__name__)


def merge_findings(results: list[AgentResult]) -> list[Finding]:
    """Deduplicate across agents, then rank by what a human should read first."""
    by_key: dict[str, Finding] = {}

    for result in results:
        for finding in result.findings:
            key = finding.dedupe_key
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = finding
                continue

            # Two specialists independently spotting the same thing is corroboration,
            # so the merged finding keeps the stronger severity and confidence and
            # records both authors.
            winner, loser = (
                (existing, finding)
                if (existing.severity.rank, existing.confidence)
                >= (finding.severity.rank, finding.confidence)
                else (finding, existing)
            )
            merged_from = sorted(
                {*winner.merged_from, *loser.merged_from, loser.agent_type} - {winner.agent_type}
            )
            by_key[key] = winner.model_copy(
                update={
                    "merged_from": merged_from,
                    "confidence": max(winner.confidence, loser.confidence),
                    "citations": sorted({*winner.citations, *loser.citations})[:10],
                }
            )

    ordered = sorted(
        by_key.values(),
        key=lambda f: (-f.severity.rank, -f.confidence, f.file_path, f.line_start),
    )
    return ordered[: settings.max_findings_posted]


def score_confidence(findings: list[Finding], results: list[AgentResult]) -> float:
    """A deterministic floor for how much to trust this review.

    Deliberately not the model's own self-assessment: this number gates whether
    a comment is posted without a human, and a model grading its own work is the
    wrong instrument for that. The model's opinion is blended in afterwards, and
    can only pull the score down.
    """
    healthy = [r for r in results if r.ok]
    if not healthy:
        return 0.0

    # A review missing two of five specialists is not a confident review, even
    # if the three that ran were sure of themselves.
    coverage = len(healthy) / max(len(results), 1)

    if not findings:
        # Nothing found, everyone reported: that is a confident clean bill.
        return round(0.85 * coverage, 3)

    weights = [f.severity.rank + 1 for f in findings]
    weighted = sum(f.confidence * w for f, w in zip(findings, weights, strict=True))
    mean_confidence = weighted / sum(weights)
    return round(min(mean_confidence * coverage, 1.0), 3)


async def summarise(
    pr: PullRequestRef,
    findings: list[Finding],
    results: list[AgentResult],
    llm: LLMClient | None = None,
) -> tuple[str, float, AgentResult]:
    """Ask a model for the author-facing summary; fall back to a written one.

    The fallback is not a nicety. If the aggregator model is unavailable we
    still have five specialists' worth of real findings, and dropping the review
    because we could not write a paragraph about it would be absurd.
    """
    deterministic = score_confidence(findings, results)
    usage_result = AgentResult(agent_type="quality", model=settings.model_aggregator)  # type: ignore[arg-type]

    degraded = [r.agent_type for r in results if not r.ok]
    body = "\n\n".join(
        f"[{f.severity.value}] {f.category} - {f.file_path}:{f.line_start}\n"
        f"{f.title}\n{f.rationale[:600]}"
        for f in findings
    ) or "No findings were reported by any specialist."

    user = (
        f"Pull request: {pr.title}\n"
        f"Repository: {pr.project}/{pr.repository_name} !{pr.pull_request_id}\n"
        f"Specialists that failed to report: {', '.join(degraded) or 'none'}\n\n"
        f"Merged findings ({len(findings)}):\n{body}\n\n"
        f"{AGGREGATOR_INSTRUCTIONS}"
    )

    try:
        response = await (llm or get_llm()).structured(
            model=settings.model_aggregator,
            system="You write concise, useful pull-request review summaries.",
            user=user,
            schema=AggregatorOutput,
            max_tokens=1200,
            effort="low",
            thinking=False,
        )
    except (LLMError, Exception) as exc:  # noqa: BLE001
        log.warning("aggregator.degraded", error=str(exc)[:200])
        usage_result.error = str(exc)
        usage_result.degraded = True
        return _fallback_summary(findings, degraded), deterministic, usage_result

    parsed: AggregatorOutput = response.parsed
    usage_result.input_tokens = response.usage.input_tokens
    usage_result.output_tokens = response.usage.output_tokens

    model_confidence = min(max(parsed.overall_confidence, 0.0), 1.0)
    # The model can lower confidence but never raise it above what the
    # deterministic score supports.
    confidence = round(min(deterministic, (deterministic + model_confidence) / 2), 3)
    summary = parsed.summary.strip() or _fallback_summary(findings, degraded)
    return summary, confidence, usage_result


def _fallback_summary(findings: list[Finding], degraded: list[str]) -> str:
    if not findings:
        text = "Automated review found no issues in the changed lines."
    else:
        counts: dict[Severity, int] = {}
        for f in findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        breakdown = ", ".join(
            f"{counts[s]} {s.value.lower()}"
            for s in sorted(counts, key=lambda s: -s.rank)
        )
        top = findings[0]
        text = (
            f"Automated review found {len(findings)} issue(s) ({breakdown}). "
            f"Start with {top.file_path or 'the pull request'}"
            f"{f':{top.line_start}' if top.line_start else ''} - {top.title}"
        )
    if degraded:
        text += (
            f" Note: the {', '.join(degraded)} specialist(s) did not complete, "
            "so coverage is partial."
        )
    return text
