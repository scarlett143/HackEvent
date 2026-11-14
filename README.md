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
| HE-003 | Caddy driven by `APP_DOMAIN`: automatic HTTPS, security headers, TLS smoke check | Done |
| HE-004 | Postgres schema (21 tables from doc 05) + Alembic baseline; upgrade/downgrade tested | Done |
| HE-005 | CI: ruff, mypy, pytest, lint, typecheck, build, dependency audits, Trivy image scan | Done |
| HE-006 | AES-256-GCM envelope encryption for secrets at rest (per-value data key wrapped by MASTER_KEY) | Done |
| HE-007 | Log redaction filter (tokens, keys, cookies, DSN passwords) on API + worker logs | Done |
| HE-011 | Password login (Argon2id), hashed sliding sessions, logout, /me, 5-per-15-min login rate limit | Done |
| HE-015 | CSRF double-submit protection (session-bound token, Origin check) on every write | Done |
| HE-016 | Audit log writer: 17 security actions, secrets dropped/redacted from metadata | Done |
| HE-030 | Gmail API email sender (refresh-token OAuth, retries, revoked-token alert) + autoescaped invite/test templates | Done |

Full ticket list: [docs/09-Feature-Ticket-List.md](docs/09-Feature-Ticket-List.md).

## Run locally

Requirements: Docker with Compose v2, [uv](https://docs.astral.sh/uv/), Node 24 + pnpm.

```bash
# checks
uv run pytest && uv run ruff check && uv run mypy services
pnpm install && pnpm lint && pnpm typecheck && pnpm build

# full stack (copy infra/.env.example to infra/.env, chmod 600, set POSTGRES_PASSWORD)
cd infra && docker compose up -d --build && ./smoke.sh
```

The stack serves the web app at `/` and the API at `/api/*` through Caddy.
`GET /api/v1/health` returns `{"db":"ok","redis":"ok","workers":"ok"}` when everything is up.

## Domain and HTTPS

Caddy (`infra/Caddyfile`) takes the domain only from `APP_DOMAIN` in `infra/.env`:

| `APP_DOMAIN` | Certificate | URLs |
|---|---|---|
| `localhost` (default) | Caddy internal CA | `https://localhost`, `https://ntfy.localhost` |
| `<name>.duckdns.org` | Let's Encrypt (automatic) | `https://<name>.duckdns.org`, `https://ntfy.<name>.duckdns.org` |

- HTTP redirects to HTTPS. `/api/*` goes to the API, `ntfy.<domain>` to ntfy, everything else to the web app.
- `ACME_EMAIL` is optional (Let's Encrypt account email).
- `HSTS=on` only once the domain is final: browsers cache it for a year.
- Every response gets `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy` and a default CSP. `Server`, `X-Powered-By` and `Via` are removed. If the web app sets its own CSP, that one is used instead.
- Google Search Console: put the `google<hex>.html` file in `infra/site-verification/` on the server (gitignored). Caddy serves it at `/google<hex>.html`.
- Local: trust the dev CA with `docker compose exec caddy cat /data/caddy/pki/authorities/local/root.crt`, or accept the browser warning. `smoke.sh` checks against this CA automatically.
- Production needs DNS pointing at the VM and ports 80/443 open (Runbook B6). `smoke.sh` then requires the certificate to be valid for more than 14 days.
