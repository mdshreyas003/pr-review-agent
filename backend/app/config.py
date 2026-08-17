"""Single source of configuration truth.

Every module reads settings from here; nothing reads os.environ directly.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AgentType = Literal["security", "quality", "tests", "docs", "story"]

# config.py -> app -> backend -> repo root
_BACKEND_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _BACKEND_DIR.parent

# Anchored to this file, not the working directory. `.env` lives at the repo
# root (that is what docker compose reads), but scripts run from `backend/` -
# a relative "\.env" silently finds nothing there and every setting falls back
# to its default, which looks like the config was ignored rather than missing.
# Later entries win, so a backend-local .env can override the shared one.
_ENV_FILES = (_REPO_ROOT / ".env", _BACKEND_DIR / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILES,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- app ----------------------------------------------------------------
    environment: Literal["local", "dev", "staging", "prod"] = "local"
    log_level: str = "INFO"
    api_base_path: str = ""

    # ---- data spine ---------------------------------------------------------
    # One Postgres. Three access shapes: vector (code_chunks, when
    # MEMORY_BACKEND=vector - the default), relational (pr_review_records /
    # finding_records / hitl_*), time-series (agent_events).
    database_url: str = "postgresql://prreview:prreview@localhost:5432/prreview"
    db_pool_min: int = 2
    db_pool_max: int = 10
    # TimescaleDB turns agent_events into a hypertable with continuous
    # aggregates. Without it we fall back to native declarative partitioning
    # and plain rollup views - same queries, same API, less automation.
    enable_timescaledb: bool = True

    redis_url: str = "redis://localhost:6379/0"
    # "redis" hands off to the ARQ worker (production). "inline" runs the
    # orchestrator in the request, which is what the end-to-end test needs to
    # assert on a finished review without racing a worker process.
    queue_mode: Literal["redis", "inline"] = "redis"

    # ---- Azure DevOps -------------------------------------------------------
    ado_org_url: str = "https://dev.azure.com/your-org"
    ado_project: str = ""
    # "pat": HTTP Basic with a personal access token (legacy). "service_principal":
    # Entra ID app registration authenticating with a client certificate (.pem),
    # via ClientCertificateCredential - no PAT to rotate or leak.
    ado_auth_mode: Literal["pat", "service_principal"] = "pat"
    ado_pat: str = ""
    ado_tenant_id: str = ""
    ado_client_id: str = ""
    # Path to the .pem holding the service principal's private key (+ cert).
    ado_client_certificate_path: str = ""
    # Only needed if the .pem's private key is password-protected.
    ado_client_certificate_password: str = ""
    ado_api_version: str = "7.1"
    # Azure DevOps service hooks authenticate with HTTP Basic, not an HMAC
    # signature. These are the credentials you enter in the subscription UI.
    ado_webhook_username: str = "prreview"
    ado_webhook_password: str = ""
    # Delivery Plan used for story mapping. Blank = resolve by name/first plan.
    ado_delivery_plan_id: str = ""
    ado_delivery_plan_name: str = ""
    # Comma-separated repository names (or GUIDs) to restrict the poller and
    # the nightly reindex cron to. Both the webhook trigger and a repo
    # explicitly passed to `--repo` / `/api/index/{id}` bypass this - it only
    # narrows the "watch everything in the project" default. Blank means what
    # it always meant: the whole project.
    ado_repositories_raw: str = Field(default="", alias="ADO_REPOSITORIES")

    @property
    def ado_repositories(self) -> list[str]:
        return [r.strip() for r in self.ado_repositories_raw.split(",") if r.strip()]

    # ---- specialists ----------------------------------------------------------
    # Comma-separated subset of security,quality,tests,docs,story to actually
    # run - `build_specialists()` filters on this every review. Blank means
    # what it always meant: run all five.
    enabled_agents_raw: str = Field(default="", alias="ENABLED_AGENTS")

    @property
    def enabled_agents(self) -> set[AgentType] | None:
        agents = {a.strip() for a in self.enabled_agents_raw.split(",") if a.strip()}
        return agents or None  # type: ignore[return-value]

    # ---- LLM ----------------------------------------------------------------
    # Every specialist runs on Kimi K2 hosted in Microsoft Foundry, via the GA
    # OpenAI /v1 surface. This is the only provider implementing the
    # `LLMClient` seam right now.
    llm_provider: Literal["azure_foundry"] = "azure_foundry"

    # ---- Azure Foundry (Kimi K2) --------------------------------------------
    # The GA OpenAI /v1 surface, not the retired Azure AI Inference SDK route.
    # Accepts either the services.ai.azure.com or openai.azure.com host.
    foundry_endpoint: str = ""  # https://<resource>.services.ai.azure.com
    foundry_deployment: str = "Kimi-K2-Thinking"
    # Leave the key blank to authenticate with Entra ID instead - a managed
    # identity in Container Apps, or your az-cli login locally. Keyless is what
    # Microsoft recommends and means there is no key to rotate.
    foundry_api_key: str = ""
    foundry_use_entra_id: bool = False
    foundry_entra_scope: str = "https://ai.azure.com/.default"
    # Kimi K2 Thinking reasons before answering, and a five-way fan-out makes
    # that latency visible. This is deliberately generous.
    foundry_timeout_seconds: float = 300.0
    # Per-agent model routing. All default to the Foundry deployment name
    # (`_apply_provider_defaults` below); override individually only for a
    # mixed setup where one agent needs a different deployment.
    model_security: str = "Kimi-K2-Thinking"
    model_quality: str = "Kimi-K2-Thinking"
    model_tests: str = "Kimi-K2-Thinking"
    model_docs: str = "Kimi-K2-Thinking"
    model_story: str = "Kimi-K2-Thinking"
    model_aggregator: str = "Kimi-K2-Thinking"
    effort_security: str = "high"
    effort_quality: str = "high"
    effort_tests: str = "medium"
    effort_docs: str = "low"
    effort_story: str = "medium"
    max_tokens_per_agent: int = 8000

    # ---- code-memory backend -------------------------------------------------
    # "vector" (default): Postgres pgvector + full-text in this project's own
    # database - no extra service to keep running, degrades to keyword-only if
    # AZURE_OPENAI_* is unset rather than failing. "mem0": hand storage,
    # embedding and search to a separate mem0 server container instead. Both
    # implementations stay in app.platform.memory; this just picks which one
    # index_files()/hybrid_search() dispatch to.
    memory_backend: Literal["vector", "mem0"] = "vector"

    # ---- embeddings (Azure OpenAI) -------------------------------------------
    # Vector backend only. Optional: without these the vector lane is skipped
    # and retrieval falls back to full-text search only - lower recall, still
    # functional.
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_embedding_deployment: str = "text-embedding-3-large"
    azure_openai_api_version: str = "2024-10-21"
    embedding_dimensions: int = 256

    # ---- mem0 (code-memory) --------------------------------------------------
    # Only consulted when MEMORY_BACKEND=mem0. Default assumes Docker Desktop
    # on the same host, reachable via the published port of an already-running
    # mem0 container - override MEM0_BASE_URL to point anywhere else (a shared
    # network alias, a remote host).
    mem0_base_url: str = "http://host.docker.internal:8888"
    mem0_api_key: str = ""

    # ---- autonomy gates -----------------------------------------------------
    # The master switch. While this is true no review is ever posted without a
    # human approving it, whatever the confidence score says. It is checked
    # before every other rule and again immediately before publishing, because
    # posting to someone else's pull request cannot be undone quietly.
    #
    # Default true: automation that comments on colleagues' work should be
    # opted into deliberately, not inherited from a default.
    require_human_approval: bool = True

    # Confidence-weighted routing, used only when require_human_approval is
    # false: high confidence + no CRITICAL posts itself, anything else queues.
    auto_post_min_confidence: float = 0.75
    escalate_on_critical: bool = True
    min_finding_confidence: float = 0.35  # below this, drop the finding entirely
    max_findings_posted: int = 25

    # ---- reliability --------------------------------------------------------
    agent_timeout_seconds: int = 180
    llm_max_retries: int = 3
    circuit_breaker_threshold: int = 5
    circuit_breaker_reset_seconds: int = 60
    max_diff_bytes: int = 400_000
    max_files_per_review: int = 60

    # ---- retrieval ----------------------------------------------------------
    retrieval_top_k: int = 8
    rrf_k: int = 60  # vector backend only - reciprocal rank fusion constant
    # Agent-controlled retrieval: the specialist gets a `search_repository` tool
    # instead of one fixed pre-fetch, and decides itself how many times (if any)
    # to call it before submitting findings. This caps the round trips so a
    # model that gets stuck searching cannot turn one review into an unbounded
    # loop of Foundry calls.
    retrieval_max_tool_calls: int = 4

    # ---- scheduled indexing --------------------------------------------------
    # Nightly, worker-driven reconciliation of the code index for every tracked
    # repository, on top of the manual on-demand indexing above. Runs once per
    # day; the cron trigger in `app.service.queue` is set for 00:00 IST (arq
    # cron schedules run in UTC, so that trigger is expressed as 18:30 UTC).
    nightly_reindex_enabled: bool = True

    @field_validator("ado_org_url", "foundry_endpoint")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @model_validator(mode="after")
    def _apply_provider_defaults(self) -> Settings:
        """Route every agent at the configured Foundry deployment.

        `FOUNDRY_DEPLOYMENT` is the single knob for the common case. Anything
        explicitly set in the environment is left alone, which is what keeps a
        mixed setup possible - a different deployment for one agent - without
        another config concept.
        """
        for field in (
            "model_security",
            "model_quality",
            "model_tests",
            "model_docs",
            "model_story",
            "model_aggregator",
        ):
            if field not in self.model_fields_set:
                setattr(self, field, self.foundry_deployment)
        return self

    @property
    def foundry_base_url(self) -> str:
        """Normalise whatever the portal showed into the chat-completions root.

        Foundry surfaces several URLs and the most prominent one is the
        *project* endpoint (``/api/projects/<name>``), which belongs to the
        Agents API and has no chat-completions route. Pasting it verbatim gets
        a 404 that looks like a bad deployment name, so the known Foundry path
        suffixes are stripped here. An unrecognised path is preserved, because
        that is how an APIM gateway in front of Foundry would look.
        """
        endpoint = self.foundry_endpoint.rstrip("/")
        if not endpoint:
            return ""  # unset stays unset, rather than becoming "/openai/v1"

        for suffix in ("/openai/v1", "/openai", "/models"):
            if endpoint.endswith(suffix):
                endpoint = endpoint[: -len(suffix)].rstrip("/")
                break
        else:
            marker = "/api/projects/"
            if marker in endpoint:
                endpoint = endpoint[: endpoint.index(marker)].rstrip("/")

        return f"{endpoint}/openai/v1"

    @property
    def foundry_configured(self) -> bool:
        has_credential = bool(self.foundry_api_key) or self.foundry_use_entra_id
        return bool(self.foundry_endpoint) and has_credential

    @model_validator(mode="after")
    def _validate_ado_auth(self) -> Settings:
        if self.ado_auth_mode == "service_principal":
            missing = [
                name
                for name, value in (
                    ("ADO_TENANT_ID", self.ado_tenant_id),
                    ("ADO_CLIENT_ID", self.ado_client_id),
                    ("ADO_CLIENT_CERTIFICATE_PATH", self.ado_client_certificate_path),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "ADO_AUTH_MODE=service_principal needs "
                    f"{', '.join(missing)} set"
                )
        return self


    @property
    def agent_models(self) -> dict[str, str]:
        return {
            "security": self.model_security,
            "quality": self.model_quality,
            "tests": self.model_tests,
            "docs": self.model_docs,
            "story": self.model_story,
        }

    @property
    def agent_effort(self) -> dict[str, str]:
        return {
            "security": self.effort_security,
            "quality": self.effort_quality,
            "tests": self.effort_tests,
            "docs": self.effort_docs,
            "story": self.effort_story,
        }

    @property
    def embeddings_configured(self) -> bool:
        return bool(self.azure_openai_endpoint and self.azure_openai_api_key)

    @property
    def mem0_configured(self) -> bool:
        return bool(self.mem0_base_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()
