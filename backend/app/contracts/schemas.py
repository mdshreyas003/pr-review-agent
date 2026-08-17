"""What the model is contractually allowed to return.

These are deliberately flatter and more permissive than `Finding`: every field
is required and non-nullable, because the structured-output schema compiler
has no notion of an optional key. Empty string and empty list are the "no
value" encodings, and the translation into the real `Finding` contract - with
its enums, bounds and defaults - happens in `app.agent.specialists` where it
can be validated and clamped.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RawFinding(BaseModel):
    severity: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
    category: str = Field(description="Short kebab-case slug, e.g. 'sql-injection'")
    file_path: str = Field(description="Repository path from the diff, or '' if not file-specific")
    line_start: int = Field(description="First affected line in post-change numbering, or 0")
    line_end: int = Field(description="Last affected line, or the same as line_start")
    title: str = Field(description="One line, under 100 characters, stating the defect")
    rationale: str = Field(description="Why this is wrong and what concretely goes wrong")
    suggestion: str = Field(description="Corrected code, or '' when you cannot write one")
    confidence: float = Field(description="0.0 to 1.0, your genuine belief this is real")
    citations: list[str] = Field(description="Chunk ids actually relied on, or []")


class SpecialistOutput(BaseModel):
    findings: list[RawFinding]


class AggregatorOutput(BaseModel):
    summary: str = Field(description="4-5 sentences for the PR author")
    overall_confidence: float = Field(description="0.0 to 1.0 trust in this review as a whole")
