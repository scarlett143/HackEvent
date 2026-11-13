# HackEvents — Product Requirements Document (PRD)

| Field | Value |
|---|---|
| Version | 0.2 (design locked) |
| Date | 2026-11-13 |
| Owner | Admin (platform owner) |
| Status | Approved design, pre-build |
| Domain | **DuckDNS** subdomain, email via Gmail API (see §9) |

---

## 1. Problem

Professors and college management miss hackathons run by companies and institutions. Announcements are spread across official sites, aggregators (Devpost, Unstop, MLH), RSS, and social media (LinkedIn, Instagram, X, Reddit, Substack). Nobody has time to check 600 organisations by hand, and registration deadlines pass without anyone noticing.

## 2. Goal

One platform that:

1. Finds hackathons from the top 300 companies and the top 300 institutions (India first, global second).
2. Shows new events the moment a user opens the app ("New since last visit").
3. Pushes alerts to every channel the user enables.
4. Reminds users before deadlines and events (3, 2, 1, 0 days by default).
5. Keeps a Google-Calendar-style calendar of the events each user registers for.

## 3. Users and roles

| Role | Count | Needs |
|---|---|---|
| **Admin** | 1 (owner) | Manage sources, tiers, LLM providers and keys, social sessions, users, invites; see source health and costs |
| **Management** | 1–3 | College-wide dashboard: what's open, who registered, weekly digest |
| **Professor** | up to ~8 | Personal feed, filters, calendar, reminders, channel settings |

Total: **1–10 users**. Sign-up is **invite-only**. Devices: **Android phones only** (Chrome) plus desktop browsers; no iOS in v1.

## 4. Scope — v1 (in)

### 4.1 Discovery
- Sources: official org pages, aggregators (Devpost, Unstop, MLH), RSS, Reddit API, Substack RSS, logged-in social scraping (LinkedIn, Instagram, X) through **one shared dedicated account per network**, managed by Admin.
- Coverage: **Tier 1–3** for top 300 institutions and, separately, top 300 companies.
  - Tier 1 daily · Tier 2 every 3 days · Tier 3 weekly · aggregators every few hours.
  - Seed lists: Fortune Global 500 + Indian product-company lists; NIRF + QS for institutions.
  - Tiers and sources are editable in the Admin portal.
- Hybrid extraction: selectors/RSS/APIs for clean sources, LLM for messy pages.
- Every event stores its **source link and a quoted snippet**. Low-confidence events are flagged "Needs review".
- Dedupe on normalised name + organiser + date. **Registration deadline and event date are stored separately.**

### 4.2 Feed
- Deadline-sorted feed, "New" badge, countdown, eligibility chips, Register link.
- Filters: organiser type (company/institution), tier, region (India/global), mode (online/offline/hybrid), deadline window, status (open/closing/closed), registered.

### 4.3 Calendar
- Full calendar: month, week, day, agenda views; drag, resize, create.
- "I registered" puts the event on the user's calendar.
- `.ics` export and "Add to Google Calendar".
- Google Calendar API push of events and reminders (per-user OAuth). **No two-way sync in v1.**

### 4.4 Notifications (all channels, each user picks)
| Channel | Notes |
|---|---|
| In-app | "New since last visit" badge + notification centre |
| Push (FCM) | Firebase Cloud Messaging: Android Chrome + desktop browsers now, native Android app later with the same backend |
| Email | **Gmail API** from a dedicated Gmail account (HTTPS, so Oracle's port-25 block doesn't apply). Also the always-on backup channel |
| Telegram bot | Free, official Bot API |
| ntfy | Self-hosted |
| Google Calendar | Event + reminders via API |

### 4.5 Reminders
- Default offsets: **3, 2, 1, 0 days** before the deadline (unregistered, still open) and before the event (registered).
- Each user can change offsets and intervals per channel.
- Each reminder is sent **exactly once** (idempotent).
- Timezone: **IST default**, per-user override. Quiet hours **off by default**.

### 4.6 LLM
- **Gemini** for bulk extraction; **Claude Haiku** for verification and fallback.
- Each provider must work **independently** — the system runs with either one alone.
- Admin portal: add Anthropic and OpenAI-compatible endpoints, keys encrypted, daily + monthly cost caps per provider, automatic fallback.

### 4.7 Platform
- Web first, **PWA** on desktop and mobile. **Light mode default.**
- Management weekly digest (email).

## 5. Out of scope — v1

- Native mobile apps (later phase).
- Two-way Google Calendar sync.
- Public sign-up, multi-college tenancy.
- Auto-registering users on hackathon sites.
- WhatsApp in any form (replaced by FCM push; decision 2026-11-13).

## 6. Success metrics

| Metric | Target (first 3 months) |
|---|---|
| Tier-1 orgs with at least one working source | ≥ 90% |
| Median time from publish to in-app appearance (Tier 1) | ≤ 24 h |
| Duplicate events visible in feed | < 2% |
| Extraction precision on reviewed sample | ≥ 95% |
| Reminders delivered at the right time | ≥ 99% |
| Reminders sent twice | 0 |
| Monthly LLM spend | Under Admin-set cap, always |
| Users missing a deadline they tracked | 0 reported |

## 7. Constraints

- Hosting: **Oracle Cloud**, single VM, Docker Compose + Caddy, **Pay As You Go account with budget alert** (avoids Always Free idle reclamation).
- Must **not** run on any shared production server (separate VM only).
- Social scraping on LinkedIn/Instagram breaks their ToS; accounts can be banned. Treated as **best-effort**, never a hard dependency.
- Never ask for or log user passwords. Social sessions stored as **encrypted cookies only**.

## 8. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Social account banned | Lose one source type | Dedicated accounts, low rate, aggregators + official sites cover the core |
| Site layout changes | Source silently returns 0 | Source-health dashboard, "0 results for N runs" alert, LLM fallback |
| LLM hallucinates dates | Wrong reminders | Source snippet required, Haiku verification, "Needs review" flag |
| LLM cost spike | Bill shock | Hard daily/monthly caps, cache by content hash |
| Oracle reclaims or suspends VM | Outage | PAYG account, nightly off-Oracle backup, documented rebuild runbook |
| Domain change after launch | Push subscriptions and OAuth break | Fix domain before real users; domain in one config value |
| Push token lost / permission denied | User misses push | Email always on as backup; Telegram/ntfy optional; invalid tokens auto-pruned |

## 9. Decision — Q19 (domain) · decided 2026-11-13

**Domain: DuckDNS subdomain** (`<name>.duckdns.org`). Email via **Gmail API** from a dedicated Gmail account.

| Requirement | Result |
|---|---|
| HTTPS | ✅ Let's Encrypt HTTP-01 via Caddy |
| FCM push | ✅ Domain added to Firebase authorised domains |
| Google OAuth / Calendar | ✅ expected: `duckdns.org` is on the Public Suffix List, so `<name>.duckdns.org` is its own registrable domain; verify via Search Console HTML-file method **[verify at setup]** |
| Email from own domain | ❌ DuckDNS has one shared TXT value and MX = A record → no SPF + DKIM. **Solved by sending from a dedicated Gmail account via Gmail API** |

Known limits: DuckDNS is a free service with occasional outages (covered by uptime monitoring). Moving domains later forces push re-subscribe and Google reconnect (≈1 min per user for 10 users).

**Upgrade path:** if college IT grants a subdomain of the college domain before launch, switch `APP_DOMAIN` to it (Runbook Part I). Domain lives in one config value.

## 10. Phases

| Phase | Content |
|---|---|
| P0 | Infra, auth, invites, roles, DB, CI |
| P1 | Sources: aggregators + RSS + official sites, extraction, dedupe, feed |
| P2 | Calendar, "I registered", .ics, Add-to-Google |
| P3 | Reminders + in-app, FCM push, email, Telegram, ntfy |
| P4 | Admin portal: LLM providers, caps, sources, tiers, health |
| P5 | Social scraping (X, Reddit, Substack, then LinkedIn/Instagram best-effort) |
| P6 | Google Calendar API push, Management dashboard + digest |
| Later | Native mobile app (reuses FCM backend), two-way calendar sync |
