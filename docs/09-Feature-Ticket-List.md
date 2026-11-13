# HackEvents — Feature Ticket List

Version 0.1 · 2026-11-13

Priority: **P0** must-have for launch · **P1** launch if time · **P2** after launch
Size: **S** ≤ 0.5 day · **M** 1–2 days · **L** 3–5 days

Definition of Done for every ticket: code + tests pass in CI + docs updated + reviewed.

---

## Epic 0 — Foundation

| ID | Title | Pri | Size | Depends | Acceptance criteria |
|---|---|---|---|---|---|
| HE-001 | Monorepo scaffold (web, api, workers, common, infra) | P0 | S | – | `pnpm build` and `uv run pytest` succeed on empty app |
| HE-002 | Docker Compose: all services, memory caps, healthchecks | P0 | M | 001 | `docker compose up` → all healthy |
| HE-003 | Caddy config driven by `APP_DOMAIN` | P0 | S | 002 | HTTPS works on temp dev domain |
| HE-004 | Postgres + Alembic baseline migration (doc 05) | P0 | M | 002 | All tables created; downgrade works |
| HE-005 | CI: lint, typecheck, tests, audits, Trivy | P0 | M | 001 | PR shows all checks |
| HE-006 | Secrets crypto module (AES-GCM envelope) | P0 | S | 004 | Round-trip test; wrong key fails |
| HE-007 | Log redaction filter | P0 | S | 001 | Test: key/cookie patterns redacted |

## Epic 1 — Auth and users

| ID | Title | Pri | Size | Depends | Acceptance criteria |
|---|---|---|---|---|---|
| HE-010 | Invite create + email + accept | P0 | M | 004, 030 | Token single-use, 72 h expiry tested |
| HE-011 | Password login, Argon2id, sessions, logout | P0 | M | 004 | Rate limit 5 / 15 min tested |
| HE-012 | Google sign-in (OIDC) matched to invite email | P1 | M | 011 | Wrong email rejected |
| HE-013 | Role dependency + ownership helper | P0 | S | 011 | Cross-user test template exists |
| HE-014 | Admin TOTP 2FA | P0 | M | 011 | Admin can't reach `/admin` without 2FA |
| HE-015 | CSRF protection | P0 | S | 011 | Write without token → 403 |
| HE-016 | Audit log writer | P0 | S | 004 | Listed actions recorded, no secrets |

## Epic 2 — Organisations and sources

| ID | Title | Pri | Size | Depends | Acceptance criteria |
|---|---|---|---|---|---|
| HE-020 | Seed CSVs: 300 companies, 300 institutions, tiers | P0 | L | 004 | Idempotent seed; counts verified |
| HE-021 | Source scheduler by tier cadence | P0 | M | 020 | Tier 1 daily, 2 every 3 days, 3 weekly |
| HE-022 | Fetcher: httpx, rate limit, robots, ETag, SSRF guard | P0 | M | 021 | SSRF tests for private ranges + redirect |
| HE-023 | RSS adapter | P0 | S | 022 | Fixture test |
| HE-024 | Devpost adapter | P0 | M | 022 | Fixture test |
| HE-025 | Unstop adapter | P0 | M | 022 | Fixture test |
| HE-026 | MLH adapter | P1 | S | 022 | Fixture test |
| HE-027 | Playwright fetcher container | P0 | M | 022 | JS page fixture renders |
| HE-028 | Reddit API adapter | P1 | M | 022 | Fixture test |
| HE-029 | Substack RSS adapter | P1 | S | 023 | Fixture test |
| HE-031 | Source health tracking + alerts | P0 | M | 021 | 5 empty / 3 error runs → alert |

## Epic 3 — Extraction and events

| ID | Title | Pri | Size | Depends | Acceptance criteria |
|---|---|---|---|---|---|
| HE-040 | LLM adapter interface + Gemini provider | P0 | M | 006 | Contract test |
| HE-041 | Anthropic (Haiku) provider | P0 | S | 040 | Contract test |
| HE-042 | OpenAI-compatible provider | P1 | S | 040 | Contract test |
| HE-043 | Cost caps + routing + fallback | P0 | M | 040 | Over-cap test → fallback used |
| HE-044 | Extraction prompt + schema + post-validation (doc 08) | P0 | L | 040 | Golden-set gate passes per provider |
| HE-045 | Haiku verification step | P0 | M | 044 | Unsupported deadline → needs_review |
| HE-046 | Content-hash cache + keyword pre-filter | P0 | S | 044 | Repeat page = 0 LLM calls |
| HE-047 | Dedupe (exact key + trigram) | P0 | M | 044 | Duplicate fixtures merge |
| HE-048 | Golden set: 50 pages incl. injection cases | P0 | L | 044 | Checked into tests |

## Epic 4 — Feed and event detail

| ID | Title | Pri | Size | Depends | Acceptance criteria |
|---|---|---|---|---|---|
| HE-050 | `GET /events` with filters + cursor | P0 | M | 047 | p95 < 300 ms on 5k events |
| HE-051 | Feed UI: sections, cards, countdown, chips | P0 | L | 050 | Matches design brief |
| HE-052 | "New since last visit" + unread count | P0 | S | 050 | Badge clears after seen |
| HE-053 | Filters UI + URL state | P0 | M | 051 | Reload keeps filters |
| HE-054 | Event detail with source + snippet | P0 | M | 050 | Snippet shown as plain text |
| HE-055 | I registered / Remind me + undo | P0 | M | 054 | Reminders recalculated |

## Epic 5 — Calendar

| ID | Title | Pri | Size | Depends | Acceptance criteria |
|---|---|---|---|---|---|
| HE-060 | Calendar API (items CRUD) | P0 | M | 055 | Ownership tests |
| HE-061 | Calendar UI: month/week/day/agenda, drag/resize/create | P0 | L | 060 | E2E: drag persists |
| HE-062 | .ics single + private feed with rotatable token | P0 | M | 060 | Imports into Google/Apple Calendar |
| HE-063 | Add to Google Calendar link | P0 | S | 054 | Opens prefilled |
| HE-064 | Google Calendar API push (insert/patch/delete + reminders) | P1 | L | 080 | Event appears with reminders |

## Epic 6 — Reminders and notifications

| ID | Title | Pri | Size | Depends | Acceptance criteria |
|---|---|---|---|---|---|
| HE-030 | Email via Gmail API (dedicated sender, `gmail.send`) + templates; wrapped as notifier plugin once 073 lands | P0 | M | 002 | Test email lands in Inbox (not spam) at Gmail and college mail; token failure alerts Admin |
| HE-070 | Reminder planner (offsets, tz, quiet hours, unique key) | P0 | M | 055 | Double-send impossible test |
| HE-071 | Reminder dispatcher (SKIP LOCKED, retries) | P0 | M | 070 | Fires within 5 min |
| HE-072 | Deadline change → recompute | P0 | S | 070 | Pending replaced, sent kept |
| HE-073 | Notifier plugin interface + in-app channel | P0 | S | 071 | Plugin contract test |
| HE-074 | FCM push: Firebase project, JS SDK + service worker, token save/refresh, `firebase-admin` sender, invalid-token pruning | P0 | M | 073 | Push received on Android Chrome (browser + installed PWA) and Chrome desktop; dead token marked invalid |
| HE-075 | Telegram bot link + send | P0 | M | 073 | `/start <code>` links chat |
| HE-076 | ntfy self-hosted + send | P1 | S | 073 | Authenticated topic only |
| HE-077 | Notification settings UI + "Send test" | P0 | M | 073 | Each channel testable |
| HE-078 | New-event alerts by user filters | P1 | S | 073 | Matching users only |
| HE-079 | ~~WhatsApp bridge~~ — **dropped 2026-11-13**, replaced by FCM (HE-074) | – | – | – | – |

## Epic 7 — Google integration

| ID | Title | Pri | Size | Depends | Acceptance criteria |
|---|---|---|---|---|---|
| HE-080 | Google OAuth incremental Calendar scope + token storage | P1 | M | 006 | Token encrypted; revoke handled |
| HE-081 | 8-day refresh-token survival test | P1 | S | 080 | Documented result (verifies TRD §8) |

## Epic 8 — Admin portal

| ID | Title | Pri | Size | Depends | Acceptance criteria |
|---|---|---|---|---|---|
| HE-090 | Users + invites UI | P0 | M | 010 | Invite → accept works end to end |
| HE-091 | Orgs + sources UI, CSV import, Run now | P0 | L | 021 | Run now shows result |
| HE-092 | Source health dashboard | P0 | M | 031 | Red/amber/green with reason |
| HE-093 | Review queue | P0 | M | 045 | Approve / edit / merge / reject |
| HE-094 | LLM providers UI: write-only keys, test, caps, usage chart | P0 | L | 043 | Key never returned by API |
| HE-095 | Social session upload + check | P1 | M | 006 | Cookies encrypted; status shown |
| HE-096 | Audit log viewer | P1 | S | 016 | Filter by actor/action |

## Epic 9 — Social sources (best-effort)

| ID | Title | Pri | Size | Depends | Acceptance criteria |
|---|---|---|---|---|---|
| HE-100 | X logged-in scraper | P1 | M | 027, 095 | Fixture + live smoke |
| HE-101 | LinkedIn company posts scraper | P2 | L | 027, 095 | Best-effort; pauses on session expiry |
| HE-102 | Instagram profile posts scraper | P2 | L | 027, 095 | Best-effort; pauses on session expiry |

## Epic 10 — Management

| ID | Title | Pri | Size | Depends | Acceptance criteria |
|---|---|---|---|---|---|
| HE-110 | Dashboard summary API + UI | P1 | M | 055 | Management + Admin only |
| HE-111 | Weekly digest email (Mon 09:00 IST) | P1 | S | 030 | Sent once per week |

## Epic 11 — PWA and polish

| ID | Title | Pri | Size | Depends | Acceptance criteria |
|---|---|---|---|---|---|
| HE-120 | Manifest, icons, install prompt, offline shell | P0 | M | 051 | Installable on Android Chrome |
| HE-121 | Accessibility pass (WCAG 2.2 AA) | P0 | M | 061 | axe: 0 serious issues |
| HE-122 | Dark mode toggle | P2 | S | 051 | Light stays default |
| HE-123 | Data export / delete (`/me/export`, `/me/delete`) | P1 | M | 013 | Export complete; delete cascades |

## Epic 12 — Ops

| ID | Title | Pri | Size | Depends | Acceptance criteria |
|---|---|---|---|---|---|
| HE-130 | Nightly pg_dump → Object Storage + off-Oracle copy | P0 | M | 004 | Restore drill passes |
| HE-131 | Uptime + disk + error alerts to Admin | P0 | S | 075 | Test alert received |
| HE-132 | Oracle VM provisioning + hardening script | P0 | M | – | Runbook Part B reproducible |
| HE-133 | DuckDNS domain setup: claim subdomain, IP updater, Firebase + Google OAuth authorised domain | P0 | S | 003 | HTTPS valid, push works, Google sign-in works on `APP_DOMAIN` |

---

## Critical path to launch

`001 → 002 → 030 → 004 → 011 → 010 → 020 → 021 → 022 → 024/025 → 040 → 044 → 047 → 050 → 051 → 055 → 070 → 071 → 073 → 074 → 120 → 130 → 133`

Rough total (P0 only): ~45–60 working days for one developer (estimate).
