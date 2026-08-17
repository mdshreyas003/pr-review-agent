"""Retry with backoff, plus a circuit breaker per upstream.

The breaker matters more than the retry: when Azure Foundry or Azure DevOps is
having a bad minute, retrying five specialist agents in parallel turns one
outage into a self-inflicted thundering herd. Opening the circuit makes the
whole review degrade fast and predictably instead.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

import structlog

from app.config import settings

log = structlog.get_logger(__name__)

T = TypeVar("T")


class CircuitOpenError(RuntimeError):
    def __init__(self, name: str, retry_after: float):
        super().__init__(f"circuit '{name}' is open; retry in {retry_after:.0f}s")
        self.name = name
        self.retry_after = retry_after


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        threshold: int | None = None,
        reset_seconds: int | None = None,
    ) -> None:
        self.name = name
        self.threshold = threshold or settings.circuit_breaker_threshold
        self.reset_seconds = reset_seconds or settings.circuit_breaker_reset_seconds
        self._failures = 0
        self._opened_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def is_open(self) -> bool:
        if self._opened_at == 0.0:
            return False
        if time.monotonic() - self._opened_at >= self.reset_seconds:
            # Half-open: let the next call through and judge by its result.
            self._opened_at = 0.0
            self._failures = 0
            log.info("circuit.half_open", circuit=self.name)
            return False
        return True

    async def check(self) -> None:
        if self.is_open:
            remaining = self.reset_seconds - (time.monotonic() - self._opened_at)
            raise CircuitOpenError(self.name, max(remaining, 0))

    async def record_success(self) -> None:
        async with self._lock:
            self._failures = 0
            self._opened_at = 0.0

    async def record_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._failures >= self.threshold and self._opened_at == 0.0:
                self._opened_at = time.monotonic()
                log.warning(
                    "circuit.opened", circuit=self.name, failures=self._failures
                )


_breakers: dict[str, CircuitBreaker] = {}


def breaker(name: str) -> CircuitBreaker:
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(name)
    return _breakers[name]


def reset_breakers() -> None:
    """Test hook - a breaker tripped by one test must not leak into the next."""
    _breakers.clear()


async def with_resilience(
    fn: Callable[[], Awaitable[T]],
    *,
    circuit: str,
    attempts: int | None = None,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    give_up_on: tuple[type[BaseException], ...] = (),
) -> T:
    """Run `fn` behind a named circuit, retrying transient failures.

    `give_up_on` exists so a 401 or a schema-validation failure fails fast
    instead of burning three attempts on something that will never succeed.
    """
    cb = breaker(circuit)
    await cb.check()
    total = attempts or settings.llm_max_retries
    last: BaseException | None = None

    for attempt in range(1, total + 1):
        try:
            result = await fn()
            await cb.record_success()
            return result
        except give_up_on:
            await cb.record_success()  # upstream is healthy; our request was bad
            raise
        except retry_on as exc:
            last = exc
            await cb.record_failure()
            if attempt >= total:
                break
            delay = min(base_delay * 2 ** (attempt - 1), max_delay)
            delay += random.uniform(0, delay * 0.25)  # jitter, noqa: S311
            log.warning(
                "resilience.retry",
                circuit=circuit,
                attempt=attempt,
                of=total,
                delay_s=round(delay, 2),
                error=str(exc)[:200],
            )
            await asyncio.sleep(delay)

    assert last is not None
    raise last
