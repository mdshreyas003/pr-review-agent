"""The audit spine, plus logging setup.

Every span, LLM call, tool call and decision lands in `agent_events`. It is
append-only by construction - there is no update or delete path in this module,
deliberately. The trace viewer, the cost ledger and drift detection are all
just different queries over these rows.

Emission never raises into the caller: losing an audit row is bad, but failing
a review because the audit write failed is worse.
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog

from app.config import settings
from app.platform import db

log = structlog.get_logger(__name__)

_trace_id: ContextVar[str] = ContextVar("trace_id", default="")
_span_id: ContextVar[str] = ContextVar("span_id", default="")
_review_id: ContextVar[str] = ContextVar("review_id", default="")

_INSERT = """
INSERT INTO agent_events (
    ts, review_id, trace_id, span_id, parent_span_id, agent_type, event_type,
    status, duration_ms, model, input_tokens, output_tokens, payload
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
"""


def new_trace_id() -> str:
    return uuid.uuid4().hex


def current_trace_id() -> str:
    return _trace_id.get()


def bind_trace(trace_id: str, review_id: UUID | str | None = None) -> None:
    _trace_id.set(trace_id)
    if review_id is not None:
        _review_id.set(str(review_id))


async def emit(
    event_type: str,
    *,
    agent_type: str = "",
    status: str = "ok",
    duration_ms: int = 0,
    model: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    review_id: UUID | str | None = None,
    span_id: str = "",
    parent_span_id: str = "",
    **payload: Any,
) -> None:
    rid = str(review_id) if review_id else _review_id.get()
    try:
        await db.execute(
            _INSERT,
            datetime.now(UTC),
            UUID(rid) if rid else None,
            _trace_id.get(),
            span_id or _span_id.get(),
            parent_span_id,
            agent_type,
            event_type,
            status,
            duration_ms,
            model,
            input_tokens,
            output_tokens,
            payload,
        )
    except Exception as exc:  # noqa: BLE001 - audit must never break the run
        log.warning("event.emit.failed", event_type=event_type, error=str(exc))


@asynccontextmanager
async def span(
    event_type: str,
    *,
    agent_type: str = "",
    review_id: UUID | str | None = None,
    **payload: Any,
):
    """Time a block and emit exactly one row for it, success or failure.

    Yields a mutable dict; anything the block puts in it is merged into the
    emitted payload, which is how agents attach token counts and costs to the
    span that produced them.
    """
    sid = uuid.uuid4().hex[:16]
    parent = _span_id.get()
    token = _span_id.set(sid)
    started = time.perf_counter()
    extra: dict[str, Any] = {}
    status = "ok"
    try:
        yield extra
    except Exception as exc:
        status = "error"
        extra.setdefault("error", str(exc))
        extra.setdefault("error_type", type(exc).__name__)
        raise
    finally:
        _span_id.reset(token)
        await emit(
            event_type,
            agent_type=agent_type,
            status=status,
            duration_ms=int((time.perf_counter() - started) * 1000),
            model=str(extra.pop("model", "")),
            input_tokens=int(extra.pop("input_tokens", 0) or 0),
            output_tokens=int(extra.pop("output_tokens", 0) or 0),
            review_id=review_id,
            span_id=sid,
            parent_span_id=parent,
            **{**payload, **extra},
        )


async def trace_for_review(review_id: UUID) -> list[dict[str, Any]]:
    """Reconstruct one review end to end, in emission order."""
    rows = await db.fetch(
        """
        SELECT ts, event_id, trace_id, span_id, parent_span_id, agent_type,
               event_type, status, duration_ms, model, input_tokens,
               output_tokens, payload
        FROM agent_events
        WHERE review_id = $1
        ORDER BY ts ASC
        """,
        review_id,
    )
    return [
        {**dict(r), "ts": r["ts"].isoformat(), "event_id": str(r["event_id"])}
        for r in rows
    ]


def configure_logging() -> None:
    """JSON in deployed environments, readable locally."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.dev.ConsoleRenderer()
        if settings.environment == "local"
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
