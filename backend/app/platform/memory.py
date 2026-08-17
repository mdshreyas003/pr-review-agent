"""The code-memory (RAG) lane: chunking, indexing, retrieval, and the
execution point that drives them end to end.

Two interchangeable storage backends, chosen by `MEMORY_BACKEND` (default
`"vector"`):

  "vector"  Postgres pgvector + full-text, hybrid retrieval via reciprocal
            rank fusion. Self-contained - the only external dependency is
            Azure OpenAI for embeddings, and even that is optional (a missing
            key just drops the vector lane, keyword search still works).
            This is the default because it has no separate service to keep
            running: the data spine already runs, so an empty
            `AZURE_OPENAI_API_KEY` is the only way to lose recall, never a
            downed container.

  "mem0"    A mem0 server container does the embedding, storage and search.
            One repository maps to one mem0 `user_id`; each indexed chunk is
            one mem0 memory with `infer=False` (stored verbatim, no LLM fact
            extraction) so retrieval returns exact code, not mem0's summary
            of it. Opt in with `MEMORY_BACKEND=mem0` plus `MEM0_BASE_URL`.

Both share the same chunker, the same content-addressed `chunk_id` scheme,
and the same public surface (`index_files`, `hybrid_search`,
`prune_repository`, `run_index`) - callers never know which backend is live.

Chunking is structural, not fixed-width: splitting on top-level definitions
keeps a function and its signature in the same chunk, which is what makes a
retrieved chunk usable as grounding rather than a fragment. Content hashes
(`chunk_id`) make re-indexing a changed repo cheap - unchanged chunks are
skipped before they reach either backend.

`run_index` owns the things that must happen exactly once around a run:
resolve a fixed commit, mark the record running, guarantee the Azure DevOps
client is closed, and guarantee the run never ends stuck in 'running'. It is
invoked by an explicit trigger through `app.service.api` (a human clicking
"reindex") or by the nightly cron job in `app.service.queue`
(`cron_nightly_reindex`) - no poller or webhook calls this.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

import httpx
import structlog

from app.config import settings
from app.contracts.azure import get_ado_client
from app.platform import db
from app.resilience import with_resilience

if TYPE_CHECKING:
    from app.contracts.azure import AzureDevOpsClient
    from app.contracts.models import RetrievedChunk

log = structlog.get_logger(__name__)

MAX_CHUNK_LINES = 120
MIN_CHUNK_LINES = 3
EMBED_BATCH = 64       # vector backend: Azure OpenAI embedding batch size
ADD_CONCURRENCY = 10   # mem0 backend: it has no batch-add-with-per-item-metadata endpoint

# ---- manual, on-demand indexing straight from Azure DevOps (no checkout) ----
INDEXABLE_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".cs", ".go", ".java", ".rb", ".php",
    ".rs", ".sql", ".sh", ".yml", ".yaml", ".razor", ".md",
}
SKIP_DIR_NAMES = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".next", "obj", "bin", ".pytest_cache", ".ruff_cache", "vendor",
}
MAX_FETCH_FILE_BYTES = 400_000
FETCH_CONCURRENCY = 10

# Language-agnostic "this line starts a new top-level thing" heuristic.
_BOUNDARY = re.compile(
    r"^\s{0,4}("
    r"(async\s+)?def\s+\w+|"
    r"class\s+\w+|"
    r"(export\s+)?(default\s+)?(async\s+)?function\s+\w+|"
    r"(export\s+)?(abstract\s+)?class\s+\w+|"
    r"(public|private|protected|internal)\s+[\w<>\[\],\s]+\s+\w+\s*\(|"
    r"func\s+\w+|"
    r"(export\s+)?(const|let|var)\s+\w+\s*=\s*(async\s*)?\("
    r")"
)

_LANG_BY_EXT = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript", ".js": "javascript",
    ".jsx": "javascript", ".cs": "csharp", ".go": "go", ".java": "java",
    ".rb": "ruby", ".php": "php", ".rs": "rust", ".sql": "sql", ".sh": "shell",
    ".yml": "yaml", ".yaml": "yaml", ".json": "json", ".md": "markdown", ".razor": "razor",
}

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")

_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "into", "not", "are",
    "was", "were", "has", "have", "had", "but", "you", "all", "can", "will",
    "def", "return", "import", "class", "const", "let", "var", "function",
    "public", "private", "static", "void", "true", "false", "null", "none",
    "self", "new", "int", "str", "bool", "list", "dict", "async", "await",
}


# ------------------------------------------------------------------ embeddings
# Vector backend only. Azure OpenAI, truncated to 256 dimensions -
# `text-embedding-3-large` supports Matryoshka truncation, and recall at 256
# is close to 3072 for code search while the index is an order of magnitude
# smaller.
class EmbeddingsUnavailable(RuntimeError):
    pass


class AzureOpenAIEmbeddings:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = client is None
        self._http = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    @property
    def dimensions(self) -> int:
        return settings.embedding_dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not settings.embeddings_configured:
            raise EmbeddingsUnavailable(
                "AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY are not set"
            )

        url = (
            f"{settings.azure_openai_endpoint.rstrip('/')}/openai/deployments/"
            f"{settings.azure_openai_embedding_deployment}/embeddings"
        )

        async def _call() -> list[list[float]]:
            resp = await self._http.post(
                url,
                params={"api-version": settings.azure_openai_api_version},
                headers={"api-key": settings.azure_openai_api_key},
                json={"input": texts, "dimensions": settings.embedding_dimensions},
            )
            resp.raise_for_status()
            data = resp.json()
            ordered = sorted(data["data"], key=lambda d: d["index"])
            return [d["embedding"] for d in ordered]

        return await with_resilience(_call, circuit="azure-openai-embeddings")


_provider: AzureOpenAIEmbeddings | None = None
_embeddings_override: object | None = None


def get_embeddings() -> AzureOpenAIEmbeddings:
    global _provider
    if _embeddings_override is not None:
        return _embeddings_override  # type: ignore[return-value]
    if _provider is None:
        _provider = AzureOpenAIEmbeddings()
    return _provider


def set_embeddings_override(provider: object | None) -> None:
    global _embeddings_override
    _embeddings_override = provider


# ------------------------------------------------------------------ mem0 client
# mem0 backend only.
class Mem0Unavailable(RuntimeError):
    pass


class Mem0Client:
    """Thin async wrapper over the mem0 self-hosted server's REST API.

    Every call is scoped to a `user_id`, which this module always sets to a
    repository id - that is mem0's native tenancy boundary, and it is what
    makes `delete_all` a clean "forget this repository" operation.
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = client is None
        headers = {"X-API-Key": settings.mem0_api_key} if settings.mem0_api_key else {}
        self._http = client or httpx.AsyncClient(
            base_url=settings.mem0_base_url.rstrip("/"),
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers=headers,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def add(self, user_id: str, content: str, metadata: dict[str, Any]) -> str:
        """Store one chunk verbatim. `infer=False` skips mem0's LLM fact
        extraction - a code chunk is grounding context, not a conversation to
        summarise, and retrieval needs the exact text back."""
        if not settings.mem0_configured:
            raise Mem0Unavailable("MEM0_BASE_URL is not set")

        async def _call() -> str:
            resp = await self._http.post(
                "/memories",
                json={
                    "messages": [{"role": "user", "content": content}],
                    "user_id": user_id,
                    "infer": False,
                    "metadata": metadata,
                },
            )
            resp.raise_for_status()
            results = resp.json().get("results") or []
            return results[0]["id"] if results else ""

        return await with_resilience(_call, circuit="mem0")

    async def list_all(self, user_id: str) -> list[dict[str, Any]]:
        """Every memory for a repository. Capped at mem0's own `top_k` ceiling
        (1000) - repositories indexing more chunks than that will see
        reconciliation and dedup degrade, not crash."""
        resp = await self._http.get(
            "/memories", params={"user_id": user_id, "top_k": 1000}
        )
        resp.raise_for_status()
        return resp.json().get("results") or []

    async def delete(self, memory_id: str) -> None:
        resp = await self._http.delete(f"/memories/{memory_id}")
        resp.raise_for_status()

    async def delete_all(self, user_id: str) -> None:
        resp = await self._http.delete("/memories", params={"user_id": user_id})
        resp.raise_for_status()

    async def search(self, user_id: str, query: str, top_k: int) -> list[dict[str, Any]]:
        if not settings.mem0_configured:
            return []

        async def _call() -> list[dict[str, Any]]:
            resp = await self._http.post(
                "/search",
                json={"query": query, "filters": {"user_id": user_id}, "top_k": top_k},
            )
            resp.raise_for_status()
            return resp.json().get("results") or []

        return await with_resilience(_call, circuit="mem0")


_client: Mem0Client | None = None
_mem0_override: object | None = None


def get_mem0_client() -> Mem0Client:
    global _client
    if _mem0_override is not None:
        return _mem0_override  # type: ignore[return-value]
    if _client is None:
        _client = Mem0Client()
    return _client


def set_mem0_client_override(client: object | None) -> None:
    global _mem0_override
    _mem0_override = client


# --------------------------------------------------------------------- chunking
# Shared by both backends.
@dataclass(slots=True)
class Chunk:
    chunk_id: str
    file_path: str
    line_start: int
    line_end: int
    symbol: str
    content: str
    content_sha: str
    language: str


def chunk_file(file_path: str, content: str, repository_id: str = "") -> list[Chunk]:
    """Split one file into chunks with globally unique, content-addressed ids.

    `repository_id` is part of the id because `chunk_id` is the primary key.
    Without it, two repositories containing the same file at the same path -
    a shared LICENSE, a copied config, vendored code, a monorepo that was split -
    produce identical ids, and the second repository's rows collide with the
    first's. The upsert then overwrites the first repository's content while the
    second never gets rows of its own, so one repo silently retrieves the
    other's code and the other retrieves nothing.
    """
    lines = content.splitlines()
    if not lines:
        return []

    language = _LANG_BY_EXT.get("." + file_path.rsplit(".", 1)[-1].lower(), "")
    definition_starts = {i for i, line in enumerate(lines) if _BOUNDARY.match(line)}
    boundaries = sorted(definition_starts | {0}) + [len(lines)]

    chunks: list[Chunk] = []
    for start, end in zip(boundaries, boundaries[1:], strict=False):
        # A definition is always worth indexing, however short. The length
        # floor exists to skip trivial preamble fragments (a lone import, a
        # run of blank lines), not to lose two-line functions - which are
        # common, and exactly the kind of thing retrieval should surface.
        if start not in definition_starts and end - start < MIN_CHUNK_LINES:
            continue
        # A very long definition is still split, so no single chunk can dominate
        # the retrieval budget.
        for offset in range(start, end, MAX_CHUNK_LINES):
            window_end = min(offset + MAX_CHUNK_LINES, end)
            body = "\n".join(lines[offset:window_end])
            if not body.strip():
                continue
            sha = hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()
            chunks.append(
                Chunk(
                    chunk_id=hashlib.sha256(
                        f"{repository_id}:{file_path}:{offset}:{sha}".encode()
                    ).hexdigest()[:40],
                    file_path=file_path,
                    line_start=offset + 1,
                    line_end=window_end,
                    symbol=_symbol_of(lines[offset:window_end]),
                    content=body,
                    content_sha=sha,
                    language=language,
                )
            )
    return chunks


def _symbol_of(lines: list[str]) -> str:
    for line in lines[:5]:
        match = re.search(r"(?:def|class|function|func)\s+(\w+)", line)
        if match:
            return match.group(1)
    return ""


def _is_indexable_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if suffix not in INDEXABLE_EXTENSIONS:
        return False
    parts = path.strip("/").split("/")
    return not any(part in SKIP_DIR_NAMES for part in parts)


async def collect_from_ado(
    client: AzureDevOpsClient, project: str, repository_id: str, commit_sha: str
) -> dict[str, str]:
    """Pull every indexable file's content straight from Azure DevOps.

    Lists the tree once at a fixed commit, then fetches file contents
    concurrently (bounded - a real repository is hundreds of individual GET
    requests, serial fetching would make this impractically slow). One file
    failing to fetch is logged and skipped rather than aborting the run.
    """
    paths = [
        p
        for p in await client.list_repository_tree(project, repository_id, commit_sha)
        if _is_indexable_path(p)
    ]

    files: dict[str, str] = {}
    semaphore = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def _fetch(path: str) -> None:
        async with semaphore:
            try:
                content = await client.get_file_content(
                    project, repository_id, path, commit_sha
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("index.ado.fetch_failed", path=path, error=str(exc)[:200])
                return
            if len(content.encode("utf-8", "replace")) > MAX_FETCH_FILE_BYTES:
                return
            files[path] = content

    await asyncio.gather(*(_fetch(p) for p in paths))
    return files


@dataclass(slots=True)
class IndexResult:
    written: int = 0
    superseded: int = 0
    removed_files: int = 0
    unchanged: int = 0

    def __int__(self) -> int:  # keeps `written` the headline number
        return self.written


# ------------------------------------------------------------------ index_files
# Public entry point; dispatches to whichever backend MEMORY_BACKEND selects.
async def index_files(
    repository_id: str,
    project: str,
    files: dict[str, str],
    commit_sha: str = "",
    *,
    embed: bool = True,
    prune_missing: bool = False,
) -> IndexResult:
    """Sync a mapping of path -> content into the configured backend.

    Chunk ids are content-addressed, so editing a function produces a *new* id
    rather than updating the old entry. Writing without reconciling therefore
    leaves the previous version of that code in the index forever, where
    retrieval will happily serve it to an agent as repository context - worse
    than having no index, because it is confidently wrong. Both backends
    reconcile per file (delete what the current content no longer produces)
    and short-circuit unchanged chunks on their content hash.

    `prune_missing` additionally drops files absent from `files` entirely, which
    handles deletions and renames. It is only safe when `files` is the whole
    repository - a partial pass would delete everything it did not look at.
    """
    if settings.memory_backend == "mem0":
        return await _index_files_mem0(
            repository_id, project, files, commit_sha, prune_missing=prune_missing
        )
    return await _index_files_vector(
        repository_id, project, files, commit_sha, embed=embed, prune_missing=prune_missing
    )


# ---- vector backend --------------------------------------------------------
async def _index_files_vector(
    repository_id: str,
    project: str,
    files: dict[str, str],
    commit_sha: str,
    *,
    embed: bool,
    prune_missing: bool,
) -> IndexResult:
    result = IndexResult()

    chunks: list[Chunk] = []
    for path, content in files.items():
        chunks.extend(chunk_file(path, content, repository_id))

    result.superseded = await _reconcile_vector(repository_id, files, chunks)
    if prune_missing:
        result.removed_files = await prune_absent_files(repository_id, list(files))

    if not chunks:
        return result

    existing = {
        r["chunk_id"]
        for r in await db.fetch(
            "SELECT chunk_id FROM code_chunks "
            "WHERE repository_id = $1 AND chunk_id = ANY($2::text[])",
            repository_id,
            [c.chunk_id for c in chunks],
        )
    }
    new_chunks = [c for c in chunks if c.chunk_id not in existing]
    result.unchanged = len(chunks) - len(new_chunks)
    if not new_chunks:
        log.info(
            "index.unchanged",
            repository_id=repository_id,
            chunks=len(chunks),
            superseded=result.superseded,
        )
        return result

    vectors: list[list[float] | None] = [None] * len(new_chunks)
    if embed:
        provider = get_embeddings()
        for i in range(0, len(new_chunks), EMBED_BATCH):
            batch = new_chunks[i : i + EMBED_BATCH]
            try:
                embedded = await provider.embed([c.content for c in batch])
                for j, vec in enumerate(embedded):
                    vectors[i + j] = vec
            except Exception as exc:  # noqa: BLE001
                # Keyword search still works without embeddings, so a failed
                # batch degrades recall instead of aborting the index.
                log.warning("index.embed.failed", batch_start=i, error=str(exc)[:200])

    rows = [
        (
            c.chunk_id, repository_id, project, c.file_path, c.line_start, c.line_end,
            c.language, c.symbol, c.content, c.content_sha, commit_sha,
            ("[" + ",".join(f"{v:.6f}" for v in vec) + "]") if vec else None,
        )
        for c, vec in zip(new_chunks, vectors, strict=True)
    ]

    async with db.get_pool().acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO code_chunks (
                chunk_id, repository_id, project, file_path, line_start, line_end,
                language, symbol, content, content_sha, commit_sha, embedding
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::vector)
            ON CONFLICT (chunk_id) DO UPDATE
                SET content = EXCLUDED.content,
                    embedding = COALESCE(EXCLUDED.embedding, code_chunks.embedding),
                    commit_sha = EXCLUDED.commit_sha,
                    indexed_at = now()
            """,
            rows,
        )
    result.written = len(rows)
    log.info(
        "index.written",
        repository_id=repository_id,
        written=result.written,
        superseded=result.superseded,
        unchanged=result.unchanged,
        removed_files=result.removed_files,
    )
    return result


async def _reconcile_vector(
    repository_id: str,
    files: dict[str, str],
    chunks: list[Chunk],
) -> int:
    """Delete chunks the current content no longer produces.

    Runs before the insert so a crash midway leaves the index smaller and
    correct rather than larger and stale.
    """
    live_by_path: dict[str, list[str]] = {}
    for chunk in chunks:
        live_by_path.setdefault(chunk.file_path, []).append(chunk.chunk_id)

    superseded = 0
    async with db.get_pool().acquire() as conn:
        for path in files:
            deleted = await conn.fetchval(
                """
                WITH gone AS (
                    DELETE FROM code_chunks
                     WHERE repository_id = $1
                       AND file_path = $2
                       AND NOT (chunk_id = ANY($3::text[]))
                    RETURNING 1
                )
                SELECT count(*) FROM gone
                """,
                repository_id,
                path,
                live_by_path.get(path, []),
            )
            superseded += int(deleted or 0)

    if superseded:
        log.info(
            "index.superseded", repository_id=repository_id, chunks=superseded
        )
    return superseded


async def prune_absent_files(repository_id: str, present_paths: list[str]) -> int:
    """Drop chunks for files that no longer exist. Whole-repository passes only.
    Vector backend only - see `_prune_absent_files_mem0` for the mem0 side."""
    deleted = await db.fetchval(
        """
        WITH gone AS (
            DELETE FROM code_chunks
             WHERE repository_id = $1
               AND NOT (file_path = ANY($2::text[]))
            RETURNING 1
        )
        SELECT count(*) FROM gone
        """,
        repository_id,
        present_paths,
    )
    return int(deleted or 0)


# ---- mem0 backend -----------------------------------------------------------
async def _index_files_mem0(
    repository_id: str,
    project: str,
    files: dict[str, str],
    commit_sha: str,
    *,
    prune_missing: bool,
) -> IndexResult:
    result = IndexResult()

    chunks: list[Chunk] = []
    for path, content in files.items():
        chunks.extend(chunk_file(path, content, repository_id))

    client = get_mem0_client()
    existing = await client.list_all(repository_id)
    existing_by_chunk_id: dict[str, str] = {}  # chunk_id -> mem0 memory id
    existing_by_path: dict[str, list[tuple[str, str]]] = {}  # file_path -> [(chunk_id, memory_id)]
    for m in existing:
        meta = m.get("metadata") or {}
        cid, fp = meta.get("chunk_id"), meta.get("file_path")
        if not cid:
            continue
        existing_by_chunk_id[cid] = m["id"]
        if fp:
            existing_by_path.setdefault(fp, []).append((cid, m["id"]))

    result.superseded = await _reconcile_mem0(client, files, chunks, existing_by_path)
    if prune_missing:
        result.removed_files = await _prune_absent_files_mem0(client, files, existing_by_path)

    new_chunks = [c for c in chunks if c.chunk_id not in existing_by_chunk_id]
    result.unchanged = len(chunks) - len(new_chunks)
    if not new_chunks:
        log.info(
            "index.unchanged",
            repository_id=repository_id,
            chunks=len(chunks),
            superseded=result.superseded,
        )
        return result

    semaphore = asyncio.Semaphore(ADD_CONCURRENCY)
    written = 0

    async def _add(c: Chunk) -> None:
        nonlocal written
        async with semaphore:
            try:
                await client.add(
                    repository_id,
                    c.content,
                    metadata={
                        "chunk_id": c.chunk_id,
                        "file_path": c.file_path,
                        "line_start": c.line_start,
                        "line_end": c.line_end,
                        "language": c.language,
                        "symbol": c.symbol,
                        "content_sha": c.content_sha,
                        "commit_sha": commit_sha,
                        "project": project,
                    },
                )
                written += 1
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "index.mem0.add_failed",
                    repository_id=repository_id,
                    file_path=c.file_path,
                    error=str(exc)[:200],
                )

    await asyncio.gather(*(_add(c) for c in new_chunks))
    result.written = written
    log.info(
        "index.written",
        repository_id=repository_id,
        written=result.written,
        superseded=result.superseded,
        unchanged=result.unchanged,
        removed_files=result.removed_files,
    )
    return result


async def _reconcile_mem0(
    client: Mem0Client,
    files: dict[str, str],
    chunks: list[Chunk],
    existing_by_path: dict[str, list[tuple[str, str]]],
) -> int:
    """Delete memories the current content no longer produces.

    Runs before the add pass so a crash midway leaves the index smaller and
    correct rather than larger and stale.
    """
    live_by_path: dict[str, set[str]] = {}
    for chunk in chunks:
        live_by_path.setdefault(chunk.file_path, set()).add(chunk.chunk_id)

    superseded = 0
    for path in files:
        live = live_by_path.get(path, set())
        for chunk_id, memory_id in existing_by_path.get(path, []):
            if chunk_id not in live:
                await client.delete(memory_id)
                superseded += 1

    if superseded:
        log.info("index.superseded", chunks=superseded)
    return superseded


async def _prune_absent_files_mem0(
    client: Mem0Client,
    files: dict[str, str],
    existing_by_path: dict[str, list[tuple[str, str]]],
) -> int:
    """Drop memories for files that no longer exist. Whole-repository passes only."""
    removed = 0
    for path, entries in existing_by_path.items():
        if path in files:
            continue
        for _, memory_id in entries:
            await client.delete(memory_id)
        removed += 1
    return removed


async def prune_repository(repository_id: str) -> int:
    if settings.memory_backend == "mem0":
        client = get_mem0_client()
        existing = await client.list_all(repository_id)
        await client.delete_all(repository_id)
        return len(existing)

    result = await db.execute(
        "DELETE FROM code_chunks WHERE repository_id = $1", repository_id
    )
    return int(result.split()[-1]) if result.startswith("DELETE") else 0


async def mem0_stats(repository_id: str) -> dict[str, Any]:
    """Chunk/file counts and freshness read live from mem0. Mem0 backend only
    - the vector backend gets the same numbers straight from `code_chunks` via
    `app.platform.repositories`, no extra round trip needed."""
    try:
        existing = await get_mem0_client().list_all(repository_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("index.mem0.stats_failed", repository_id=repository_id, error=str(exc)[:200])
        return {"chunk_count": 0, "file_count": 0, "last_indexed_at": None}

    files = {(m.get("metadata") or {}).get("file_path") for m in existing}
    files.discard(None)
    timestamps = [m.get("updated_at") for m in existing if m.get("updated_at")]
    return {
        "chunk_count": len(existing),
        "file_count": len(files),
        "last_indexed_at": max(timestamps) if timestamps else None,
    }


# --------------------------------------------------------------------- retrieval
async def hybrid_search(
    repository_id: str,
    query: str,
    *,
    top_k: int | None = None,
    exclude_paths: set[str] | None = None,
) -> list[RetrievedChunk]:
    """Retrieval failing is not review failing: an empty context list means the
    agents reason over the diff alone, which is worse but still useful."""
    if settings.memory_backend == "mem0":
        return await _hybrid_search_mem0(
            repository_id, query, top_k=top_k, exclude_paths=exclude_paths
        )
    return await _hybrid_search_vector(
        repository_id, query, top_k=top_k, exclude_paths=exclude_paths
    )


# ---- vector backend --------------------------------------------------------
async def _hybrid_search_vector(
    repository_id: str,
    query: str,
    *,
    top_k: int | None,
    exclude_paths: set[str] | None,
) -> list[RetrievedChunk]:
    top_k = top_k or settings.retrieval_top_k
    fetch_n = top_k * 3  # over-fetch per lane so fusion has something to work with

    vector_hits = await _vector_search(repository_id, query, fetch_n)
    keyword_hits = await _keyword_search(repository_id, query, fetch_n)

    if not vector_hits and not keyword_hits:
        return []

    fused = _reciprocal_rank_fusion([vector_hits, keyword_hits], k=settings.rrf_k)
    if exclude_paths:
        # The diff itself is already in the prompt; re-retrieving it wastes
        # context budget that grounding context should be using.
        fused = [c for c in fused if c.file_path not in exclude_paths]
    return fused[:top_k]


async def _vector_search(repository_id: str, query: str, limit: int) -> list[RetrievedChunk]:
    from app.contracts.models import RetrievedChunk

    try:
        vectors = await get_embeddings().embed([query])
    except (EmbeddingsUnavailable, Exception) as exc:  # noqa: BLE001
        log.info("retrieval.vector.skipped", reason=str(exc)[:200])
        return []
    if not vectors:
        return []

    literal = "[" + ",".join(f"{v:.6f}" for v in vectors[0]) + "]"
    try:
        rows = await db.fetch(
            """
            SELECT chunk_id, file_path, line_start, line_end, content,
                   1 - (embedding <=> $2::vector) AS score
            FROM code_chunks
            WHERE repository_id = $1 AND embedding IS NOT NULL
            ORDER BY embedding <=> $2::vector
            LIMIT $3
            """,
            repository_id,
            literal,
            limit,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("retrieval.vector.failed", error=str(exc)[:200])
        return []

    return [
        RetrievedChunk(
            chunk_id=r["chunk_id"],
            file_path=r["file_path"],
            line_start=r["line_start"],
            line_end=r["line_end"],
            content=r["content"],
            score=float(r["score"]),
            source="vector",
        )
        for r in rows
    ]


async def _keyword_search(repository_id: str, query: str, limit: int) -> list[RetrievedChunk]:
    from app.contracts.models import RetrievedChunk

    terms = _identifiers(query)
    if not terms:
        return []
    tsquery = " | ".join(terms)
    try:
        rows = await db.fetch(
            """
            SELECT chunk_id, file_path, line_start, line_end, content,
                   ts_rank(content_tsv, query) AS score
            FROM code_chunks, to_tsquery('english', $2) AS query
            WHERE repository_id = $1 AND content_tsv @@ query
            ORDER BY score DESC
            LIMIT $3
            """,
            repository_id,
            tsquery,
            limit,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("retrieval.keyword.failed", error=str(exc)[:200])
        return []

    return [
        RetrievedChunk(
            chunk_id=r["chunk_id"],
            file_path=r["file_path"],
            line_start=r["line_start"],
            line_end=r["line_end"],
            content=r["content"],
            score=float(r["score"]),
            source="keyword",
        )
        for r in rows
    ]


def _identifiers(query: str, limit: int = 24) -> list[str]:
    """Extract searchable identifiers, dropping diff punctuation and stopwords."""
    seen: dict[str, None] = {}
    for match in _IDENTIFIER.findall(query):
        lowered = match.lower()
        if lowered in _STOPWORDS or len(lowered) < 3:
            continue
        seen.setdefault(lowered, None)
        if len(seen) >= limit:
            break
    return list(seen)


def _reciprocal_rank_fusion(
    rankings: list[list[RetrievedChunk]], k: int = 60
) -> list[RetrievedChunk]:
    """Merge ranked lists by 1/(k+rank), ignoring the incomparable raw scores."""
    scores: dict[str, float] = {}
    chunks: dict[str, RetrievedChunk] = {}
    for ranking in rankings:
        for rank, chunk in enumerate(ranking, start=1):
            scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + 1.0 / (k + rank)
            chunks.setdefault(chunk.chunk_id, chunk)

    fused: list[RetrievedChunk] = []
    for chunk_id, score in sorted(scores.items(), key=lambda kv: kv[1], reverse=True):
        chunk = chunks[chunk_id].model_copy(update={"score": score, "source": "hybrid"})
        fused.append(chunk)
    return fused


# ---- mem0 backend -----------------------------------------------------------
async def _hybrid_search_mem0(
    repository_id: str,
    query: str,
    *,
    top_k: int | None,
    exclude_paths: set[str] | None,
) -> list[RetrievedChunk]:
    from app.contracts.models import RetrievedChunk

    top_k = top_k or settings.retrieval_top_k
    fetch_n = top_k * 3 if exclude_paths else top_k  # over-fetch only if post-filtering

    try:
        results = await get_mem0_client().search(repository_id, query, fetch_n)
    except Exception as exc:  # noqa: BLE001
        log.warning("retrieval.mem0.failed", error=str(exc)[:200])
        return []

    chunks = [
        RetrievedChunk(
            chunk_id=(r.get("metadata") or {}).get("chunk_id") or r["id"],
            file_path=(r.get("metadata") or {}).get("file_path", ""),
            line_start=int((r.get("metadata") or {}).get("line_start") or 0),
            line_end=int((r.get("metadata") or {}).get("line_end") or 0),
            content=r.get("memory", ""),
            score=float(r.get("score") or 0.0),
            source="mem0",
        )
        for r in results
    ]
    if exclude_paths:
        # The diff itself is already in the prompt; re-retrieving it wastes
        # context budget that grounding context should be using.
        chunks = [c for c in chunks if c.file_path not in exclude_paths]
    return chunks[:top_k]


# ----------------------------------------------------------------- index runner
# Triggered manually via app/service/api.py, or nightly via the
# `cron_nightly_reindex` job in app/service/queue.py. Never by a poller or
# webhook.
async def run_index(
    run_id: UUID,
    project: str,
    repository_id: str,
    branch: str,
    *,
    replace_existing: bool,
    embed_requested: bool,
    commit_sha: str = "",
) -> None:
    from app.platform import repositories as repo

    client = get_ado_client()
    try:
        resolved_branch = branch
        if not resolved_branch:
            meta = await client.get_repository(project, repository_id)
            resolved_branch = (meta.get("defaultBranch") or "refs/heads/main").replace(
                "refs/heads/", ""
            )
        commit = commit_sha or await client.resolve_branch_commit(
            project, repository_id, resolved_branch
        )
        await repo.mark_index_run_running(run_id, resolved_branch, commit)

        files = await collect_from_ado(client, project, repository_id, commit)

        if replace_existing:
            await prune_repository(repository_id)

        # A whole-branch pass, so files that vanished since the last run are
        # dropped too - otherwise deleted code stays retrievable forever.
        # What "effective" means depends on the backend: for vector it's
        # whether Azure OpenAI is configured; for mem0, whether the container
        # is reachable to embed into (mem0 always embeds server-side, there is
        # no keyword-only fallback to opt into on that side).
        configured = (
            settings.mem0_configured
            if settings.memory_backend == "mem0"
            else settings.embeddings_configured
        )
        embed_effective = embed_requested and configured
        result = await index_files(
            repository_id,
            project,
            files,
            commit,
            embed=embed_effective,
            prune_missing=True,
        )
        await repo.complete_index_run(
            run_id, result, files_collected=len(files), embed_effective=embed_effective
        )
        log.info(
            "index.run.completed",
            run_id=str(run_id),
            repository_id=repository_id,
            written=result.written,
            files=len(files),
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("index.run.failed", run_id=str(run_id), repository_id=repository_id)
        await repo.fail_index_run(run_id, f"{type(exc).__name__}: {exc}")
        raise
    finally:
        await client.aclose()
