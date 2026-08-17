# CLAUDE.md

AI PR review agent for Azure DevOps: `backend/` (Python/FastAPI) + `frontend/` (Next.js),
run via root `docker-compose.yml`.

## Layout

- `backend/app/contracts/` — shared models, LLM protocol, Azure DevOps client, webhook + poller triggers.
- `backend/app/agent/` — prompts, five specialists, aggregator, workflow engine, HITL gate.
- `backend/app/platform/` — db pool, repositories, SQL migrations, RAG memory, observability spine.
- `backend/app/service/` — REST API for the dashboard + ARQ queue/worker.
- Dependency direction: `contracts` at the bottom; `agent`/`platform` depend on it; `service` at the edge depends on all three.
- Deployment is Docker Compose only (`docker-compose.yml`); `postgres-init/` seeds the extra pytest database on first boot.

## Commands

- Python stack: `docker compose up -d --build` (migrations run on API startup; `/health` confirms schema).
- Poller (no public URL needed): `cd backend && python -m app.contracts.triggers --project X --once --dry-run`.
- Config: `.env` (see `.env.example` for the annotated list); `backend/app/config.py` is the single settings source of truth.

## Rules

- **Never run `docker compose down -v`** or otherwise delete Docker volumes — the Postgres volume holds unreproducible review history. `docker compose down` only.
- `REQUIRE_HUMAN_APPROVAL` stays `true` by default; turning it off makes the agent post comments on real PRs autonomously.
- `FOUNDRY_DEPLOYMENT` is the *deployment* name, not the model name — a mismatch is a 404.
- No bundled Python test suite; verify with the poller `--dry-run` against a real project.
- Specialists never raise into the orchestrator — degradation is a returned value, not an exception.
