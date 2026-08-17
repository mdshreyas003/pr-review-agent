"""Which model answers which agent, and the provider that answers it.

Routing is config, not code: security and quality reason hardest and get the
most effort; docs is near-mechanical and gets the least. `set_llm_override` is
what a scripted-model test uses to run the real orchestrator with no network.

The one provider implementation of the `LLMClient` seam lives here:

  * `AzureFoundryLLM` - Kimi K2 on Microsoft Foundry, targeting the GA
    **OpenAI /v1** surface. Auth two ways (API key, or Entra ID via a managed
    identity), and a downgrade ladder for structured output that isn't
    guaranteed: strict schema, then JSON mode, then schema-in-the-prompt with
    best-effort extraction, because Kimi K2 Thinking reasons before answering
    and the content can arrive with a preamble.

`to_strict_schema` normalises a Pydantic schema into the strict-mode JSON
Schema subset the API accepts. `extract_json_object` is the conservative
best-effort JSON recovery `AzureFoundryLLM` uses on the last rung of the ladder
- it extracts and unwraps, it never invents missing fields.

When `structured()` is called with `tools`, the model drives its own retrieval:
its final answer is collected as one more tool call (`submit_findings`, whose
parameters are the same strict schema) rather than via `response_format`, so
"call a tool" and "produce structured output" stay one mechanism. A deployment
that rejects tool calling downgrades to the plain, no-tools path automatically
(`_structured_with_tools` raising `_UnsupportedFormat`, caught in
`structured()`) rather than failing the specialist.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any, Literal, TypeVar

import httpx
import structlog
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.contracts.llm_protocol import (
    LLMClient,
    LLMError,
    LLMResponse,
    LLMUsage,
    ToolExecutor,
    ToolSpec,
)
from app.resilience import with_resilience

log = structlog.get_logger(__name__)

T = TypeVar("T", bound=BaseModel)


# ------------------------------------------------------------------- routing
_client: LLMClient | None = None
_override: LLMClient | None = None


def model_for(agent_type: str) -> str:
    return settings.agent_models.get(agent_type, settings.model_quality)


def effort_for(agent_type: str) -> str:
    return settings.agent_effort.get(agent_type, "high")


def get_llm() -> LLMClient:
    global _client
    if _override is not None:
        return _override
    if _client is None:
        _client = AzureFoundryLLM()
    return _client


def reset_llm_cache() -> None:
    """Drop the memoised client so a provider or endpoint change takes effect."""
    global _client
    _client = None


def set_llm_override(client: LLMClient | None) -> None:
    """Install a stand-in client. Tests use this; production never calls it."""
    global _override
    _override = client


# --------------------------------------------------------------------- schema
# Rejected by the structured-outputs schema compiler. We still enforce them
# client-side, because Pydantic validates the parsed result afterwards.
_UNSUPPORTED = {
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "uniqueItems",
    "pattern",
    "default",
}


def to_strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Turn a Pydantic model into a schema the structured-outputs API will accept.

    The API supports a deliberate subset of JSON Schema: every object must set
    `additionalProperties: false` and list every property as required, and the
    numeric/length constraint keywords are rejected. Pydantic emits all of
    those, so we normalise here rather than hand-writing schemas per agent and
    letting them drift from the models we actually validate against.
    """
    return _normalise(model.model_json_schema())


def _normalise(node: Any) -> Any:
    if isinstance(node, list):
        return [_normalise(item) for item in node]
    if not isinstance(node, dict):
        return node

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _UNSUPPORTED:
            continue
        out[key] = _normalise(value)

    if out.get("type") == "object" or "properties" in out:
        out["type"] = "object"
        out["additionalProperties"] = False
        props = out.get("properties") or {}
        # Strict mode has no notion of an optional key; defaults are expressed
        # by the model instructing the agent to send "" or [].
        out["required"] = list(props.keys())
    return out


# ---------------------------------------------------------------- json_extract
# Reasoning models sometimes inline their scratchpad in the content rather than
# a separate field. The answer is what follows the closing tag.
_FENCE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.DOTALL)
_THINK_BLOCK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    """Drop inline <think> spans so they cannot be mistaken for the answer."""
    return _THINK_BLOCK.sub("", text)


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort recovery of a single JSON object from model prose."""
    if not text or not text.strip():
        return None

    cleaned = strip_reasoning(text)
    for candidate in _json_candidates(cleaned):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            # A bare array where an object was requested is a common slip.
            # Tagged rather than guessed at, so the caller decides.
            return {"__list__": parsed}
    return None


def _json_candidates(text: str) -> list[str]:
    """Ordered attempts, most-likely-correct first."""
    out: list[str] = [text.strip()]
    out.extend(match.strip() for match in _FENCE.findall(text))
    for opener, closer in (("{", "}"), ("[", "]")):
        span = _balanced_span(text, opener, closer)
        if span:
            out.append(span)

    seen: set[str] = set()
    return [c for c in out if c and not (c in seen or seen.add(c))]


def _balanced_span(text: str, opener: str, closer: str) -> str | None:
    """First balanced bracket span, ignoring brackets inside string literals."""
    start = text.find(opener)
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def coerce_to_schema_shape(payload: dict[str, Any], expected_key: str) -> dict[str, Any]:
    """Nudge a near-miss into the shape the schema expects.

    Renaming a key is safe; inventing one is not, so an unrecognised shape is
    returned untouched and left to fail validation loudly.
    """
    if expected_key in payload:
        return payload

    if "__list__" in payload:
        return {expected_key: payload["__list__"]}

    # Exactly one key holding a list: almost certainly the right data under the
    # wrong name.
    if len(payload) == 1:
        ((_, only_value),) = payload.items()
        if isinstance(only_value, list):
            return {expected_key: only_value}

    return payload


def cacheable(text: str) -> dict[str, Any]:
    """A plain text content block.

    Named `cacheable` because ordering still matters: put shared, byte-identical
    blocks first and agent-specific instructions last, in case a future provider
    behind this seam does prompt caching. Foundry has no such mechanism today,
    so this is currently just a text block.
    """
    return {"type": "text", "text": text}


# ---------------------------------------------------------------- Azure Foundry
Mode = Literal["json_schema", "json_object", "prompt"]

# Downgrade order. A deployment that rejects strict schema gets JSON mode; one
# that rejects that too gets the schema in the prompt and JSON extraction.
_LADDER: tuple[Mode, ...] = ("json_schema", "json_object", "prompt")

# Refresh an Entra token a little before it actually expires, so a long
# fan-out cannot straddle the boundary.
_TOKEN_SKEW_SECONDS = 300


class AzureFoundryLLM(LLMClient):
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        use_entra_id: bool | None = None,
    ) -> None:
        # `is not None` rather than truthiness, matching api_key below: an
        # explicitly-empty argument means empty, not "fall back to settings".
        # Otherwise the meaning of the call depends on whether a .env exists.
        resolved = base_url if base_url is not None else settings.foundry_base_url
        self.base_url = resolved.rstrip("/")
        self.api_key = api_key if api_key is not None else settings.foundry_api_key
        self.use_entra_id = (
            settings.foundry_use_entra_id if use_entra_id is None else use_entra_id
        )
        if not self.base_url or self.base_url == "/openai/v1":
            raise LLMError(
                "FOUNDRY_ENDPOINT is not set "
                "(expected https://<resource>.services.ai.azure.com)"
            )
        if not self.api_key and not self.use_entra_id:
            raise LLMError(
                "No Foundry credential: set FOUNDRY_API_KEY, or "
                "FOUNDRY_USE_ENTRA_ID=true to use a managed identity"
            )

        self._owns_client = client is None
        self._http = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.foundry_timeout_seconds, connect=15.0)
        )
        self._mode: dict[str, Mode] = {}
        self._credential: Any = None
        self._token: str = ""
        self._token_expires_at: float = 0.0

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()
        if self._credential is not None:
            await self._credential.close()

    # -------------------------------------------------------------------- auth
    async def _auth_headers(self) -> dict[str, str]:
        if not self.use_entra_id:
            return {"api-key": self.api_key}

        now = time.time()
        if not self._token or now >= self._token_expires_at - _TOKEN_SKEW_SECONDS:
            self._token, self._token_expires_at = await self._fetch_token()
        return {"Authorization": f"Bearer {self._token}"}

    async def _fetch_token(self) -> tuple[str, float]:
        try:
            from azure.identity.aio import DefaultAzureCredential
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise LLMError(
                "Entra ID auth needs the azure-identity package "
                "(pip install azure-identity)"
            ) from exc

        if self._credential is None:
            # Resolves managed identity in Container Apps, az-cli login locally.
            self._credential = DefaultAzureCredential()
        try:
            token = await self._credential.get_token(settings.foundry_entra_scope)
        except Exception as exc:  # noqa: BLE001
            raise LLMError(
                f"Could not obtain an Entra token for {settings.foundry_entra_scope}. "
                "In Azure, check the managed identity has the 'Cognitive Services "
                "OpenAI User' role on the Foundry resource; locally, run `az login`. "
                f"({exc})"
            ) from exc
        log.info("foundry.token.acquired", expires_in=int(token.expires_on - time.time()))
        return token.token, float(token.expires_on)

    # --------------------------------------------------------------- inference
    async def structured(
        self,
        *,
        model: str,
        system: list[dict[str, Any]] | str,
        user: str | list[dict[str, Any]],
        schema: type[T],
        max_tokens: int = 8000,
        effort: str = "high",
        thinking: bool = True,
        tools: list[ToolSpec] | None = None,
        tool_executor: ToolExecutor | None = None,
        max_tool_rounds: int = 4,
    ) -> LLMResponse:
        # `effort` and `thinking` are accepted for interface parity with
        # `LLMClient` but not forwarded: Kimi K2 Thinking reasons on its own,
        # and Foundry rejects unknown fields.
        if tools:
            try:
                return await self._structured_with_tools(
                    model, system, user, schema, max_tokens, tools, tool_executor, max_tool_rounds
                )
            except _UnsupportedFormat as exc:
                # This deployment doesn't do tool calling. Fall through to the
                # plain path below - the agent loses the ability to search on
                # its own, but still produces a review rather than failing
                # the whole specialist.
                log.info("foundry.tools.unsupported", model=model, detail=str(exc)[:200])

        return await self._structured_plain(model, system, user, schema, max_tokens)

    async def _structured_plain(
        self,
        model: str,
        system: list[dict[str, Any]] | str,
        user: str | list[dict[str, Any]],
        schema: type[T],
        max_tokens: int,
    ) -> LLMResponse:
        system_text = _flatten(system)
        user_text = _flatten(user)
        json_schema = to_strict_schema(schema)
        expected_key = next(iter(json_schema.get("properties", {})), "")

        start_at = _LADDER.index(self._mode.get(model, _LADDER[0]))
        last_error: Exception | None = None

        for mode in _LADDER[start_at:]:
            try:
                text, usage = await self._call(
                    model, system_text, user_text, json_schema, max_tokens, mode
                )
            except _UnsupportedFormat as exc:
                log.info(
                    "foundry.format.unsupported",
                    model=model,
                    mode=mode,
                    detail=str(exc)[:200],
                )
                last_error = exc
                continue

            parsed = self._parse(text, schema, expected_key, model, mode)
            if parsed is None:
                last_error = LLMError(f"{model} returned no usable JSON in '{mode}' mode")
                if mode == "prompt":
                    break  # last resort; a parse failure here is a real failure
                continue

            if self._mode.get(model) != mode:
                log.info("foundry.format.selected", model=model, mode=mode)
            self._mode[model] = mode
            return LLMResponse(
                parsed=parsed, model=model, usage=usage, stop_reason="stop", raw_text=text
            )

        raise LLMError(
            f"Foundry deployment '{model}' could not produce schema-valid output "
            f"({last_error})"
        )

    async def _call(
        self,
        model: str,
        system_text: str,
        user_text: str,
        json_schema: dict[str, Any],
        max_tokens: int,
        mode: Mode,
    ) -> tuple[str, LLMUsage]:
        content = user_text
        body: dict[str, Any] = {
            "model": model,  # the Foundry *deployment* name
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }

        if mode == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "specialist_output",
                    "schema": json_schema,
                    "strict": True,
                },
            }
        else:
            if mode == "json_object":
                body["response_format"] = {"type": "json_object"}
            content = _with_schema_instruction(user_text, json_schema)

        body["messages"] = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": content},
        ]

        headers = {"Content-Type": "application/json", **await self._auth_headers()}

        async def _post() -> httpx.Response:
            response = await self._http.post(
                f"{self.base_url}/chat/completions", json=body, headers=headers
            )
            self._check_status(
                response,
                model=model,
                unsupported_check=_looks_like_format_rejection if mode != "prompt" else None,
            )
            return response

        response = await with_resilience(
            _post,
            circuit=f"azure-foundry:{model}",
            give_up_on=(
                _UnsupportedFormat,
                _AuthFailure,
                _DeploymentMissing,
                _BadRequest,
            ),
        )

        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"Foundry returned no choices for '{model}'")

        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        finish = choices[0].get("finish_reason") or ""

        if finish == "content_filter":
            raise LLMError(
                f"Foundry content filter blocked the response for '{model}'. "
                "Kimi models are deployed with Protected Material Detection; a "
                "diff that echoes well-known source can trip it."
            )
        if not text.strip():
            # Reasoning models can put everything in reasoning_content and
            # leave content empty when truncated.
            if message.get("reasoning_content"):
                raise LLMError(
                    f"'{model}' returned only reasoning and no answer "
                    f"(finish_reason={finish}); raise MAX_TOKENS_PER_AGENT"
                )
            raise LLMError(f"'{model}' returned empty content (finish_reason={finish})")
        if finish == "length":
            raise LLMError(
                f"'{model}' hit the token limit before finishing its JSON; "
                "raise MAX_TOKENS_PER_AGENT"
            )

        raw_usage = data.get("usage") or {}
        input_tokens = int(raw_usage.get("prompt_tokens", 0) or 0)
        output_tokens = int(raw_usage.get("completion_tokens", 0) or 0)
        return text, LLMUsage(input_tokens=input_tokens, output_tokens=output_tokens)

    def _check_status(
        self,
        response: httpx.Response,
        *,
        model: str = "",
        unsupported_check: Callable[[str], bool] | None = None,
    ) -> None:
        """Shared HTTP status handling for both the plain and tool-calling paths.

        `unsupported_check`, when given, turns a 400 whose body matches it into
        `_UnsupportedFormat` - a permanent, fast "no" the caller can downgrade
        from instead of retrying - rather than a generic `_BadRequest`.
        """
        if response.status_code == 400:
            detail = _error_detail(response)
            if unsupported_check is not None and unsupported_check(detail):
                raise _UnsupportedFormat(detail)
            raise _BadRequest(f"Foundry rejected the request: {detail}")
        if response.status_code in (401, 403):
            raise _AuthFailure(
                f"Foundry returned {response.status_code}: {_error_detail(response)}. "
                + (
                    "Check the managed identity has the 'Cognitive Services OpenAI "
                    "User' role on the resource."
                    if self.use_entra_id
                    else "Check FOUNDRY_API_KEY."
                )
            )
        if response.status_code == 404:
            raise _DeploymentMissing(
                f"No deployment named '{model}' at {self.base_url}. "
                "FOUNDRY_DEPLOYMENT must match the deployment name in "
                "the Foundry portal, not the model name."
            )
        if response.status_code >= 500:
            # Keep the body: Foundry puts capacity and content-filter
            # explanations here, and losing them makes triage guesswork.
            raise _UpstreamError(f"Foundry {response.status_code}: {_error_detail(response)}")
        response.raise_for_status()

    # ---------------------------------------------------------- tool calling
    async def _structured_with_tools(
        self,
        model: str,
        system: list[dict[str, Any]] | str,
        user: str | list[dict[str, Any]],
        schema: type[T],
        max_tokens: int,
        tools: list[ToolSpec],
        tool_executor: ToolExecutor | None,
        max_tool_rounds: int,
    ) -> LLMResponse:
        """Let the model call tools (e.g. repository search) as many times as it
        wants, in whatever order it wants, before submitting its final answer.

        The final answer is collected as a tool call too (`submit_findings`,
        parameters = the same strict schema `structured()` would otherwise pass
        via `response_format`), not as free text - that keeps "call a tool" and
        "produce structured output" as one mechanism instead of two, and sidesteps
        whether this deployment supports combining `tools` with `response_format`
        at all. On the last allowed round, `tool_choice` is pinned to
        `submit_findings` so a model that keeps searching is forced to wrap up
        rather than run forever.
        """
        system_text = _flatten(system)
        user_text = _flatten(user)
        json_schema = to_strict_schema(schema)
        expected_key = next(iter(json_schema.get("properties", {})), "")

        submit_name = "submit_findings"
        tool_defs = [_tool_def(t) for t in tools]
        tool_defs.append(
            {
                "type": "function",
                "function": {
                    "name": submit_name,
                    "description": (
                        "Submit your finished review. Call this exactly once, when "
                        "you are done - after any repository searches you needed, "
                        "not before."
                    ),
                    "parameters": json_schema,
                    "strict": True,
                },
            }
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ]
        usage_total = LLMUsage()
        headers = {"Content-Type": "application/json", **await self._auth_headers()}

        for round_index in range(max_tool_rounds + 1):
            force_submit = round_index == max_tool_rounds
            body: dict[str, Any] = {
                "model": model,
                "max_tokens": max_tokens,
                "temperature": 0.2,
                "messages": messages,
                "tools": tool_defs,
                "tool_choice": (
                    {"type": "function", "function": {"name": submit_name}}
                    if force_submit
                    else "auto"
                ),
            }

            async def _post() -> httpx.Response:
                response = await self._http.post(
                    f"{self.base_url}/chat/completions", json=body, headers=headers
                )
                self._check_status(
                    response, model=model, unsupported_check=_looks_like_tools_rejection
                )
                return response

            response = await with_resilience(
                _post,
                circuit=f"azure-foundry:{model}",
                give_up_on=(_UnsupportedFormat, _AuthFailure, _DeploymentMissing, _BadRequest),
            )

            data = response.json()
            choices = data.get("choices") or []
            if not choices:
                raise LLMError(f"Foundry returned no choices for '{model}'")

            message = choices[0].get("message") or {}
            finish = choices[0].get("finish_reason") or ""
            raw_usage = data.get("usage") or {}
            usage_total = usage_total + LLMUsage(
                input_tokens=int(raw_usage.get("prompt_tokens", 0) or 0),
                output_tokens=int(raw_usage.get("completion_tokens", 0) or 0),
            )

            if finish == "content_filter":
                raise LLMError(
                    f"Foundry content filter blocked the response for '{model}'. "
                    "Kimi models are deployed with Protected Material Detection; a "
                    "diff that echoes well-known source can trip it."
                )

            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                # No call at all - try to recover a bare JSON answer rather than
                # failing outright, the same last-resort the plain path takes.
                text = message.get("content") or ""
                parsed = (
                    self._parse(text, schema, expected_key, model, "prompt")
                    if text.strip()
                    else None
                )
                if parsed is not None:
                    return LLMResponse(
                        parsed=parsed, model=model, usage=usage_total,
                        stop_reason=finish, raw_text=text,
                    )
                raise LLMError(
                    f"'{model}' returned no tool call and no usable JSON "
                    f"(finish_reason={finish})"
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": tool_calls,
                }
            )

            submitted_text: str | None = None
            for call in tool_calls:
                fn = call.get("function") or {}
                name = fn.get("name", "")
                call_id = call.get("id", "")
                if name == submit_name:
                    submitted_text = fn.get("arguments") or "{}"
                    # A tool_call_id needs a matching tool result even when it's
                    # the terminal call, or the next request (if any) is malformed.
                    messages.append(
                        {"role": "tool", "tool_call_id": call_id, "content": "received"}
                    )
                    continue

                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                if tool_executor is None:
                    result_text = f"Tool '{name}' is not available in this context."
                else:
                    try:
                        result_text = await tool_executor(name, args)
                    except Exception as exc:  # noqa: BLE001
                        result_text = f"Tool '{name}' failed: {exc}"
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": (result_text or "")[:8000],
                    }
                )

            if submitted_text is not None:
                parsed = self._parse(submitted_text, schema, expected_key, model, "json_schema")
                if parsed is None:
                    raise LLMError(
                        f"'{model}' submitted findings that did not validate against the schema"
                    )
                return LLMResponse(
                    parsed=parsed, model=model, usage=usage_total,
                    stop_reason="tool_call", raw_text=submitted_text,
                )
            # Only search-type tools were called this round - loop, feeding
            # their results back in for the model's next turn.

        raise LLMError(
            f"'{model}' did not submit findings within {max_tool_rounds} tool-call rounds"
        )

    def _parse(
        self, text: str, schema: type[T], expected_key: str, model: str, mode: Mode
    ) -> T | None:
        raw = extract_json_object(text)
        if raw is None:
            log.info("foundry.parse.no_json", model=model, mode=mode, sample=text[:200])
            return None
        if expected_key:
            raw = coerce_to_schema_shape(raw, expected_key)
        try:
            return schema.model_validate(raw)
        except ValidationError as exc:
            log.info(
                "foundry.parse.schema_mismatch",
                model=model,
                mode=mode,
                errors=exc.error_count(),
                sample=json.dumps(raw)[:200],
            )
            return None


# All of these subclass LLMError so callers only ever have to catch one type -
# that is the contract of the LLMClient seam. The subclasses exist so the
# retry policy can tell "this will never work" from "try again".
class _UnsupportedFormat(LLMError):
    pass


class _AuthFailure(LLMError):
    pass


class _DeploymentMissing(LLMError):
    pass


class _BadRequest(LLMError):
    pass


class _UpstreamError(LLMError):
    pass


# --------------------------------------------------------------------- helpers
def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        return response.text[:400]
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error)[:400]
    return str(error or payload)[:400]


def _looks_like_format_rejection(detail: str) -> bool:
    lowered = detail.lower()
    return "response_format" in lowered or "json_schema" in lowered or (
        "unsupported" in lowered and "format" in lowered
    )


def _looks_like_tools_rejection(detail: str) -> bool:
    """Best-effort match for 'this deployment doesn't support tool calling'
    rejections, so `structured()` can downgrade to the no-tools path instead
    of surfacing a generic bad-request to the specialist."""
    lowered = detail.lower()
    return "tool_choice" in lowered or "tools" in lowered or "function_call" in lowered


def _tool_def(spec: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        },
    }


def _flatten(content: str | list[dict[str, Any]]) -> str:
    """Collapse a list of text content blocks into one string.

    Agents build prompts as ordered blocks (see `cacheable`); Foundry's chat
    API takes a single string per message, so the blocks just concatenate.
    """
    if isinstance(content, str):
        return content
    return "\n\n".join(
        block.get("text", "") for block in content if block.get("type", "text") == "text"
    )


def _with_schema_instruction(user_text: str, json_schema: dict[str, Any]) -> str:
    """Append the schema for deployments that cannot enforce it themselves."""
    return (
        f"{user_text}\n\n"
        "## Response format\n"
        "Reply with a single JSON object and nothing else. No prose before or "
        "after it, and no markdown code fence. It must validate against this "
        "JSON Schema exactly: every property is required, and no extra "
        "properties are allowed.\n\n"
        f"{json.dumps(json_schema, indent=2)}\n\n"
        "If you have nothing to report, return the object with an empty array."
    )
