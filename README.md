# HackEvent

[![CI](https://github.com/scarlett143/HackEvent/actions/workflows/ci.yml/badge.svg)](https://github.com/scarlett143/HackEvent/actions/workflows/ci.yml)

Hackathon discovery and reminder platform for professors and college management.

**Build order:** web application first. Mobile work (PWA install, FCM push, Android) comes after the web app ships.

Design docs: [docs/](docs/README.md) — PRD, TRD, schema, API, security, AI guardrails, tickets, runbook.

## Progress

| Ticket | What | Status |
|---|---|---|
| HE-001 | Monorepo scaffold (Next.js web, FastAPI api, workers, common) | Done |
| HE-002 | Docker Compose stack: 10 services, memory caps, healthchecks, smoke test | Done |
| HE-005 | CI: ruff, mypy, pytest, lint, typecheck, build, dependency audits, Trivy image scan | Done |
| HE-007 | Log redaction filter (tokens, keys, cookies, DSN passwords) on API + worker logs | Done |

Full ticket list: [docs/09-Feature-Ticket-List.md](docs/09-Feature-Ticket-List.md).

## Run locally

Requirements: Docker with Compose v2, [uv](https://docs.astral.sh/uv/), Node 24 + pnpm.

```bash
# checks
uv run pytest && uv run ruff check && uv run mypy services
pnpm install && pnpm lint && pnpm typecheck && pnpm build

# full stack (copy infra/.env.example to infra/.env first and fill it in)
cd infra && docker compose up -d --build && ./smoke.sh
```

The stack serves the web app at `/` and the API at `/api/*` through Caddy.
`GET /api/v1/health` returns `{"db":"ok","redis":"ok","workers":"ok"}` when everything is up.
