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
| HE-010 | Admin invites: hashed single-use 72 h token, invite email via NotifyWorker, public accept → user + session, audited | Done |
| HE-011 | Password login (Argon2id), hashed sliding sessions, logout, /me, 5-per-15-min login rate limit | Done |
| HE-012 | Google sign-in (OIDC, code + PKCE S256, nonce, state bound to an HttpOnly cookie, ID token verified against Google's JWKS): only an existing active user with the same verified email, Google account bound on first use; audited; "Continue with Google" on the login page when configured | Done |
| HE-013 | `require_role` dependency, row-ownership helper, deny-by-default route check, cross-user test template | Done |
| HE-014 | Admin TOTP 2FA (RFC 6238, encrypted secret, replay + rate-limit protection); /admin blocked until verified | Done |
| HE-015 | CSRF double-submit protection (session-bound token, Origin check) on every write | Done |
| HE-016 | Audit log writer: 17 security actions, secrets dropped/redacted from metadata | Done |
| HE-020 | Seed data: 300 companies (Fortune Global 500 2026, Fortune India 500 2025) + 300 institutions (NIRF 2025 Engineering), tiers, idempotent loader | Done |
| HE-021 | Source scheduler: tier cadence (1 d / 3 d / 7 d), SKIP LOCKED + job-id dedupe, DB-free scrape worker hand-off | Done |
| HE-022 | Guarded fetcher: SSRF guard (re-checked per redirect, IP pinning), 5 MB / 20 s caps, robots.txt, ETag, per-host rate limit | Done |
| HE-023 | RSS/Atom adapter (XXE-safe, plain text) | Done |
| HE-024 | Devpost adapter (public listing JSON, fixture-tested) | Done |
| HE-025 | Unstop adapter (fixture-tested) | Done |
| HE-026 | MLH adapter: season events page (Inertia JSON), exact UTC start/end, mode, location | Done |
| HE-027 | Playwright browser container (internal network, no egress); `selector_config.render` sources rendered with every request intercepted and fetched through the SSRF guard | Done |
| HE-028 | Reddit via the official API: app-only OAuth token (Redis-cached), `oauth.reddit.com/r/<sub>/new` through the guarded fetch, listing adapter; needs REDDIT_CLIENT_ID/SECRET to run | Done |
| HE-029 | Substack `/feed` via the RSS adapter; feed items without a doc 08 hackathon keyword are dropped | Done |
| HE-030 | Gmail API email sender (refresh-token OAuth, retries, revoked-token alert) + autoescaped invite/test templates | Done |
| HE-030 SMTP | SMTP sender (runbook B7.5, e.g. Gmail app password): STARTTLS 587 / TLS 465 with verified certificates, retries, rejected login = permanent; `SMTP_HOST` set switches every email to SMTP | Done |
| HE-031 | Source health: green/amber/red with reason; one admin alert on 3 errors / 5 empty runs | Done |
| HE-040 | LLM adapter (JSON-only, no tools, single call_llm choke point) + Gemini provider, contract-tested | Done |
| HE-041 | Anthropic Haiku provider (structured outputs, effort low), same contract suite | Done |
| HE-042 | OpenAI-compatible provider (Chat Completions, json_schema response format, refusal/truncation → invalid output), same contract suite | Done |
| HE-043 | LLM cost caps (fail-closed, atomic reservation), role/priority routing, fallback, usage ledger | Done |
| HE-044 | Extraction: untrusted-delimited prompt (hidden text stripped), doc 08 A3 schema v1, A5 post-validation in code, A10 injection tests | Done |
| HE-045 | Verify step: confidence < 0.7 → verify provider (Haiku), corrections re-validated against the page, unsupported deadline → needs_review | Done |
| HE-046 | Keyword pre-filter (no keyword → 0 LLM calls) + `llm_cache` by page-text hash + schema version; repeat page = 0 LLM calls | Done |
| HE-047 | Dedupe: exact key (normalised title + organiser + UTC start day) + pg_trgm fuzzy pass (≥ 0.9 merge, 0.8–0.9 → needs_review); gap-filling merge, race-safe upsert | Done |
| HE-047 wiring | Ingest pipeline: changed page → adapter (rss/api) or LLM `extract` → `upsert_candidate` → source health; LLM outage retries then forces a refetch | Done |
| HE-048 | LLM golden set: 50 labelled synthetic pages (official, aggregator, social, decoys, 8 injection cases), A9 scorer + per-provider `-m golden` gate and workflow; billed provider runs pending keys | Done |
| HE-050 | Events API: filters, keyset cursor, p95 < 300 ms on 5k events | Done |
| HE-051 | Feed UI: sections, cards, countdown, chips (design brief) | Done |
| HE-052 | "New since last visit": `is_new` / `new_only`, `GET /me/unread-count` badge, `POST /me/seen` after 3 s of visible feed + Mark all read | Done |
| HE-053 | Feed filters with URL state (reload keeps filters) | Done |
| HE-054 | Event detail: two dated countdowns, needs-review banner, source block with plain-text snippet | Done |
| HE-055 | I registered / Remind me (optimistic, 10 s undo), "Did you register?" prompt when returning from the organiser's page; reminder recompute hook filled by HE-070 | Done |
| HE-070 | Reminder planner: register -> event-date offsets, Remind me -> deadline offsets, at the user's send hour in their timezone, quiet hours respected; unique (user, event, kind, offset, channel), sent reminders never re-armed; replans on prefs / timezone / quiet hours / deactivation | Done |
| HE-071 | Reminder dispatcher: scheduler sweep every minute (SKIP LOCKED) -> notify worker; at-most-once claim before delivery; in-app + email (Gmail, backoff, 5 attempts); stale reminders cancelled | Done |
| HE-072 | Deadline / start / status change at the source replans every registered or tracking user's reminders (pending replaced, sent kept); "Deadline updated" chip for 48 h | Done |
| HE-073 | Notifier plugin interface (doc 06 B3: send/test -> SendResult, permanent_failure) with in-app + email plugins and a contract test; `GET /notifications`, `POST /notifications/read`; Alerts page shows reminders and marks them read | Done |
| HE-077 | Settings: profile, timezone, reminder offsets, quiet hours, channels with Send test (email, in-app), password change | Done |
| HE-078 | New-event alerts: a new trusted event (created `auto`, or approved from the review queue) alerts active users whose `new_event_alerts` is on and whose region / kind / mode filters match (same rules as the feed filters), in-app and/or email per channel prefs; once per event per user (inbox check + Redis claim); prefs API exposes the toggle and filters | Done |
| HE-090 | Admin users + invites UI (login, 2FA, invite, accept, role/deactivate) | Done |
| HE-091 | Admin orgs + sources UI: SSRF-checked URLs, CSV import, Run now | Done |
| HE-092 | Source health dashboard (red/amber/green with reason) | Done |
| HE-093 | Review queue: needs-review events with source snippets, reasons and likely duplicates; Approve, Edit + approve, Merge with, Reject (audited, reminders replanned) | Done |
| HE-110 | Management dashboard: `GET /dashboard/summary` (Management + Admin; professors 403) with open / closing-this-week / new-this-week counts, who registered for closing events, registrations by person and organiser type; `/dashboard` page, nav link for those roles only | Done |
| HE-111 | Weekly digest email, Monday 09:00 IST (arq cron 03:30 UTC, scheduler `timezone = UTC`): dashboard summary to active Management + Admin, dates in each recipient's timezone, names only (no addresses); once per IST ISO week via a Redis `SET NX` claim | Done |
| Feed | Priority feed: only hackathons from Fortune-listed companies (seeded Fortune Global 500 / India 500) and tier 1 colleges (NIRF 1–50) by default (`scope=all` for everything), organisers matched to the seed lists (IIT/NIT expansion, brand aliases such as Google → Alphabet); aggregators and tier 1 sources crawled hourly; feed reloads every 30 min plus a Refresh button; Admin → Changes lists new events, moved deadlines and cancellations, with Refresh now (10 min cooldown) | Done |
| HE-131 | Ops alerts to Admin (email + in-app): scheduler heartbeat, worker health, disk > 80%, reminder lag; once per incident (2 failed runs), re-armed on recovery; `scripts.send_test_alert` | Done |

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
