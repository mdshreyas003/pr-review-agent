"""Async Postgres pool + migration runner.

Raw asyncpg rather than an ORM: the three access shapes (vector search,
relational reads, hypertable inserts) each want hand-written SQL, and an ORM
would only get in the way of the pgvector and TimescaleDB bits. Vector search
is the default code-memory backend (MEMORY_BACKEND=vector) - see
app.platform.memory for the mem0 alternative.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import asyncpg
import structlog

from app.config import settings

log = structlog.get_logger(__name__)

_pool: asyncpg.Pool | None = None

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


async def _init_connection(conn: asyncpg.Connection) -> None:
    # jsonb <-> dict without callers hand-rolling json.dumps everywhere.
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )
    await conn.set_type_codec(
        "json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            init=_init_connection,
            command_timeout=30,
        )
        log.info("db.pool.created", min=settings.db_pool_min, max=settings.db_pool_max)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        log.info("db.pool.closed")


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialised; call init_pool() first")
    return _pool


async def fetch(query: str, *args: Any) -> list[asyncpg.Record]:
    async with get_pool().acquire() as conn:
        return await conn.fetch(query, *args)


async def fetchrow(query: str, *args: Any) -> asyncpg.Record | None:
    async with get_pool().acquire() as conn:
        return await conn.fetchrow(query, *args)


async def fetchval(query: str, *args: Any) -> Any:
    async with get_pool().acquire() as conn:
        return await conn.fetchval(query, *args)


async def execute(query: str, *args: Any) -> str:
    async with get_pool().acquire() as conn:
        return await conn.execute(query, *args)


async def run_migrations() -> list[str]:
    """Apply every unapplied .sql file in migrations/, in filename order.

    Each file runs inside a transaction and is recorded in schema_migrations,
    so re-running on a live database is a no-op.
    """
    pool = await init_pool()
    applied: list[str] = []
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     TEXT PRIMARY KEY,
                applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        done = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            sql = path.read_text(encoding="utf-8")
            if "{{TIMESCALE}}" in sql:
                sql = _apply_timescale_toggle(sql, await _timescale_available(conn))
            if "-- @no-transaction" in sql:
                # TimescaleDB continuous aggregates refuse to be created inside
                # a transaction block, so those migrations opt out of one.
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES ($1)", path.name
                )
            else:
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES ($1)", path.name
                    )
            applied.append(path.name)
            log.info("db.migration.applied", version=path.name)
    return applied


async def _timescale_available(conn: asyncpg.Connection) -> bool:
    if not settings.enable_timescaledb:
        return False
    return bool(
        await conn.fetchval(
            "SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'"
        )
    )


def _apply_timescale_toggle(sql: str, available: bool) -> str:
    """Strip or keep the TimescaleDB-only blocks in a migration.

    Blocks look like:
        -- {{TIMESCALE}}
        ... timescale-only SQL ...
        -- {{/TIMESCALE}}
    Plain Postgres keeps working - it just loses the automatic rollups.
    """
    out, keep = [], True
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("-- {{TIMESCALE}}"):
            keep = available
            continue
        if stripped.startswith("-- {{/TIMESCALE}}"):
            keep = True
            continue
        if keep:
            out.append(line)
    return "\n".join(out)
