# HackEvents — Technical Requirements Document (TRD)

Version 0.1 · 2026-11-13 · Domain: `APP_DOMAIN` = DuckDNS subdomain (Q19 decided)

---

## 1. Architecture

```
                         USERS (1-10: Admin / Management / Professor)
                 ┌───────────────────────────────────────────────┐
                 │  Browser / Installed PWA (desktop + mobile)   │
                 │  Next.js · TypeScript · Tailwind · shadcn/ui  │
                 └───────────────┬───────────────────────────────┘
                                 │ HTTPS (APP_DOMAIN, Let's Encrypt)
 ┌───────────────────────────────▼──────────────────────────────────────────┐
 │ ORACLE VM (PAYG, budget alert) · Docker Compose · ingress 80/443 only    │
 │                                                                          │
 │  caddy ──► web (Next.js)                                                 │
 │        └─► api (FastAPI) ─────────────► postgres                         │
 │               │  auth, REST, ics,          (all state, encrypted secrets)│
 │               │  admin                          ▲                        │
 │               ▼                                 │                        │
 │            redis (queue + locks + rate limits)  │                        │
 │               │                                 │                        │
 │      ┌────────┴────────┬─────────────────┬──────┴─────────┐              │
 │   scheduler        worker-scrape     worker-llm      worker-notify       │
 │   (tier cron,      (httpx +          (adapter:       (plugins: push,     │
 │    reminders)       Playwright)       Gemini/Haiku/   email, telegram,   │
 │                                       OpenAI-compat)  ntfy, gcal)        │
 │   ntfy (self-hosted)                  FCM (Google) ◄── worker-notify     │
 └──────────────────────────────────────────────────────────────────────────┘
   Backups: nightly pg_dump ─► Oracle Object Storage + one off-Oracle copy
```

## 2. Stack

| Layer | Choice | Reason |
|---|---|---|
| Frontend | Next.js (App Router), TypeScript, Tailwind, shadcn/ui | PWA support, one codebase for desktop + mobile |
| Calendar UI | FullCalendar (or Schedule-X) | Month/week/day/agenda, drag/resize/create built in |
| PWA | Service worker + Web App Manifest + Firebase JS SDK (`firebase/messaging`) | Install, offline shell, FCM web push |
| API | FastAPI (Python 3.12), Pydantic v2 | Same language as scrapers and LLM code; typed schemas; OpenAPI out of the box |
| ORM / migrations | SQLAlchemy 2 + Alembic | Standard, typed |
| DB | PostgreSQL 16 | Relational data, JSONB for raw payloads, `pg_trgm` for fuzzy dedupe |
| Queue | Redis 7 + ARQ (async) | Light; Celery only if ARQ falls short |
| Scraping | httpx + selectolax (static), Playwright (JS pages, social) | Cheap path first, browser only when needed |
| LLM | Thin adapter (or LiteLLM) | One interface over Gemini, Anthropic, OpenAI-compatible |
| Proxy / TLS | Caddy | Automatic HTTPS |
| Runtime | Docker Compose | Single VM, simple rebuild |
| Email | Gmail API (`users.messages.send`, scope `gmail.send`) from a dedicated Gmail account | DuckDNS can't carry SPF/DKIM; Gmail's own domain is trusted; HTTPS avoids Oracle's port-25 block; free |
| Push | Firebase Cloud Messaging, `firebase-admin` (HTTP v1 API) | One token system for web now and the native Android app later; free |

## 3. Services (Compose)

| Service | Role | Memory cap (start) |
|---|---|---|
| `caddy` | Reverse proxy + TLS | 128 MB |
| `web` | Next.js standalone build | 512 MB |
| `api` | FastAPI (uvicorn, 2 workers) | 512 MB |
| `scheduler` | ARQ cron: tier jobs, reminder sweep, digests, backups | 256 MB |
| `worker-scrape` | httpx + Playwright; concurrency 2 | 1.5 GB |
| `worker-llm` | Extraction + verification; concurrency 4 | 512 MB |
| `worker-notify` | Channel plugins | 256 MB |
| `postgres` | Data | 1 GB |
| `redis` | Queue | 256 MB |
| `ntfy` | Self-hosted push | 128 MB |

Every service has `mem_limit`, `cpus`, `restart: unless-stopped`, healthcheck.

## 4. Pipelines

### 4.1 Discovery
1. `scheduler` enqueues `scrape_source(source_id)` by tier cadence (`next_run_at <= now()`).
2. `worker-scrape` fetches with per-domain rate limit (Redis token bucket), robots.txt respected for non-social sources, conditional GET (ETag / Last-Modified).
3. Content hash compared to last run → unchanged = stop.
4. Parse:
   - `adapter = rss | api | selector` → structured directly.
   - `adapter = llm` → enqueue `extract(raw_document_id)`.
5. `worker-llm` extracts to the strict JSON schema (doc 08); confidence < 0.7 or missing deadline → Haiku verification → still low = `needs_review`.
6. Dedupe → upsert `events`, link `event_sources`.
7. New event → in-app notification fan-out + "new event" alerts to users whose filters match.

### 4.2 Reminders
1. On register / event change / prefs change → recompute `reminders` rows for affected users (`scheduled_at`, `channel`, `kind`, `offset_days`).
2. `scheduler` every minute: `SELECT ... WHERE scheduled_at <= now() AND status='pending' FOR UPDATE SKIP LOCKED`.
3. Quiet hours → push `scheduled_at` to end of quiet window.
4. Enqueue `send(reminder_id)`; unique key `(user_id, event_id, kind, offset_days, channel)` makes double-send impossible.
5. Deadline moved → delete pending, recompute. Already-sent stays sent.

### 4.3 Dedupe
- `dedupe_key = sha256(normalise(title) | normalise(organiser) | event_start::date)`.
- Normalise: lowercase, strip punctuation, remove year/edition tokens ("2026", "season 3"), collapse whitespace.
- Fuzzy pass: `pg_trgm similarity(title) > 0.8` and same organiser and dates within 3 days → merge candidate. Auto-merge if score high, else `needs_review`.

## 5. Non-functional requirements

| Area | Requirement |
|---|---|
| Users | 1–10 concurrent |
| Feed API | p95 < 300 ms |
| Page load (PWA, warm) | < 2 s on 4G |
| Availability | Best-effort single VM; RPO 24 h, RTO 4 h |
| Reminder timing | Within 5 min of `scheduled_at` |
| Scrape volume | ~600 orgs × 1–3 sources; tier mix ⇒ roughly 200–400 jobs/day plus aggregator sweeps (estimate) |
| LLM spend | Hard cap per provider per day/month; fails closed when cap hit, fallback provider used |
| Accessibility | WCAG 2.2 AA |
| Devices | **Users are Android only.** Mobile: Chrome on Android (primary target). Desktop: last 2 versions of Chrome, Edge, Firefox. iOS/Safari not supported in v1 |

## 6. Configuration

All in `.env` (never committed), loaded by Pydantic Settings.

| Key | Purpose |
|---|---|
| `APP_DOMAIN` | **Single source of truth for the domain** — `<name>.duckdns.org` |
| `DUCKDNS_TOKEN` | DuckDNS account token (secret; only for IP updates) |
| `DATABASE_URL`, `REDIS_URL` | Data |
| `MASTER_KEY` | Encrypts secrets at rest (doc 07) |
| `SESSION_SECRET` | Session signing |
| `FIREBASE_SERVICE_ACCOUNT_FILE` | Path to service-account JSON (secret, `chmod 600`, outside repo) |
| `NEXT_PUBLIC_FIREBASE_CONFIG` | Firebase web config (public by design) |
| `NEXT_PUBLIC_FCM_VAPID_KEY` | Web Push certificate public key from Firebase console (public) |
| `GMAIL_SENDER` | Dedicated Gmail address used as From |
| `GMAIL_OAUTH_REFRESH_TOKEN` | Refresh token for the sender account, `gmail.send` scope (secret) |
| `TELEGRAM_BOT_TOKEN` | Telegram |
| `NTFY_BASE_URL`, `NTFY_TOKEN` | ntfy |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | OAuth + Calendar |
| `DEFAULT_TZ` | `Asia/Kolkata` |

LLM keys are **not** env vars — they live encrypted in the DB, managed in the Admin portal.

## 7. Repo layout

```
hackevents/
├── apps/web/            Next.js PWA
├── services/api/        FastAPI app (routers, models, schemas)
├── services/workers/    scrape, llm, notify, scheduler
├── services/common/     db, crypto, llm adapter, notifier plugins
├── infra/               docker-compose.yml, Caddyfile, backup scripts, smoke.sh
├── seeds/               companies.csv, institutions.csv, sources.csv
└── docs/                these 10 documents
```

## 8. Unverified assumptions (check before relying on them)

| Assumption | Check |
|---|---|
| Oracle Always Free reclamation thresholds and PAYG behaviour | Oracle official docs at provisioning time |
| Oracle Ampere A1 free shape size (OCPU/RAM) | Oracle console when creating the VM |
| Google OAuth "In production" unverified mode avoids 7-day refresh-token expiry | Google OAuth docs + a real 8-day test (HE-081) |
| Gemini / Haiku prices | Provider pricing pages; set caps accordingly |
| Google accepts `<name>.duckdns.org` as OAuth authorised domain | Search Console HTML-file verification at setup |
| Gmail API daily send limit for a consumer account | Google docs; 10 users need well under 100/day |
| Gmail sender refresh token does not expire when OAuth app is "In production" (sensitive scope, unverified) | Same 8-day test as HE-081; fallback = Gmail SMTP on 587 with app password |
