# AI PR Review Agent — Azure DevOps

GitHub mirror: <https://github.com/mdshreyas003/pr-review-agent> · Architecture docs: <https://mdshreyas003.github.io/pr-review-agent/>

Multi-agent pull-request reviewer for Azure DevOps Repos. Five specialists (security, quality,
tests, docs, Boards story) review each PR grounded in hybrid RAG over the repository; findings are
deduped, confidence-scored, gated for human approval, and posted back as inline comment threads.
Every step lands in a time-ordered audit spine that feeds the dashboard.

FastAPI backend + Next.js dashboard, both containerized; Postgres and Redis under them.

## Run

```bash
cp .env.example .env   # fill FOUNDRY_*, ADO_* (PAT or service principal), ADO_WEBHOOK_PASSWORD
docker compose up -d --build
```

Dashboard <http://localhost:3001> · API health <http://localhost:8000/health> · OpenAPI `/docs`.
Trigger reviews via an ADO service hook to `/webhooks/azure-devops`, or without a public URL:
`cd backend && python -m app.contracts.triggers --project MyProject --interval 60`.

## Specs

- LLM: Kimi K2 on Azure Foundry (default) or Claude (`LLM_PROVIDER=anthropic`); per-agent `MODEL_*` routing; prompt caching.
- Retrieval: Postgres pgvector HNSW + full-text, fused with reciprocal rank fusion (default); mem0 optional via `MEMORY_BACKEND=mem0`. TimescaleDB optional.
- Safety: `REQUIRE_HUMAN_APPROVAL=true` by default, `DAILY_BUDGET_USD` hard cap, idempotent triggers, per-specialist degradation.
- Deploy: Docker Compose (API, worker, dashboard, Postgres, Redis) — see `docker-compose.yml`.
