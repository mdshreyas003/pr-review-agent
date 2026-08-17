"""FastAPI application.

Two surfaces: the webhook ingress that Azure DevOps posts to, and the read API
the dashboard consumes. The heavy work happens in the worker, so this process
stays small and fast to restart.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.contracts.triggers import router as webhook_router
from app.platform import db
from app.platform.observability import configure_logging
from app.service.api import index_router, router as api_router
from app.service.queue import close_queue

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    await db.init_pool()
    await db.run_migrations()
    log.info("api.started", environment=settings.environment, queue_mode=settings.queue_mode)
    try:
        yield
    finally:
        await close_queue()
        await db.close_pool()
        log.info("api.stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="AI PR Review Agent",
        version="0.1.0",
        description="Multi-agent pull-request review for Azure DevOps Repos and Boards.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.environment == "local" else [],
        allow_origin_regex=None if settings.environment == "local" else r"https://.*",
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(webhook_router)
    app.include_router(api_router)
    app.include_router(index_router)

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, Any]:
        """Liveness plus a real dependency check - a pod that cannot reach
        Postgres is not healthy, however cheerfully it responds."""
        try:
            await db.fetchval("SELECT 1")
            database = "ok"
        except Exception as exc:  # noqa: BLE001
            database = f"error: {exc}"
        healthy = database == "ok"
        return JSONResponse(  # type: ignore[return-value]
            status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "ok" if healthy else "degraded",
                "database": database,
                "environment": settings.environment,
                "version": app.version,
            },
        )

    @app.exception_handler(Exception) #Global exception handler for unhandled exceptions
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("api.unhandled", path=request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error"},
        )

    return app


app = create_app()
