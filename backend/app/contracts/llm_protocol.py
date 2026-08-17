"""The seam between agents and whichever model actually answers them.

Agents depend on `LLMClient`, never on a provider SDK directly. That is what
lets a scripted-model test run the full orchestrator with no network, and what
would let a future provider swap happen in one file (`app.agent.llm`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass(slots=True)
class ToolSpec:
    """One tool a model may call mid-reasoning, e.g. repository search.

    `parameters` is a JSON Schema object (same subset `to_strict_schema`
    produces for structured output) describing the call's arguments.
    """

    name: str
    description: str
    parameters: dict[str, Any]


# Given a tool name and its parsed arguments, run it and return the result as
# text to feed back to the model. Implemented by the caller (a specialist),
# not the LLM client - the client only knows how to shuttle tool calls back
# and forth, not what `search_repository` means.
ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[str]]


@dataclass(slots=True)
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: LLMUsage) -> LLMUsage:
        return LLMUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


@dataclass(slots=True)
class LLMResponse:
    """A parsed, typed answer plus its token usage."""

    parsed: Any
    model: str
    usage: LLMUsage = field(default_factory=LLMUsage)
    stop_reason: str = ""
    raw_text: str = ""


class LLMError(RuntimeError):
    """Raised when the model could not be reached or produced unusable output."""


class LLMClient(Protocol):
    async def structured(
        self,
        *,
        model: str,
        system: list[dict[str, Any]] | str,
        user: str,
        schema: type[T],
        max_tokens: int = 8000,
        effort: str = "high",
        thinking: bool = True,
        tools: list[ToolSpec] | None = None,
        tool_executor: ToolExecutor | None = None,
        max_tool_rounds: int = 4,
    ) -> LLMResponse:
        """Ask `model` for output matching `schema`, and report token usage.

        When `tools` is given (with `tool_executor` to run them), the model
        may call them zero or more times - deciding itself what to fetch and
        when - before it submits output matching `schema`. Without `tools`
        this is a single request/response call, unchanged from before tool
        support existed.
        """
        ...
