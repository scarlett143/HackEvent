# HackEvents — API Integrations and Specifications

Version 0.1 · 2026-11-13 · Base URL `https://APP_DOMAIN/api/v1`

---

## Part A — Internal REST API

Conventions
- JSON, UTF-8. Times ISO 8601 UTC.
- Auth: `HttpOnly; Secure; SameSite=Lax` session cookie + CSRF header `X-CSRF-Token` on writes.
- Errors: `{"error": {"code": "string", "message": "string", "details": {}}}`.
- Pagination: cursor — `?cursor=…&limit=50`, response `{ "items": [], "next_cursor": null }`.
- OpenAPI served at `/api/v1/openapi.json` (Admin only in production).

### A1. Auth
| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/auth/csrf` | public | `{ "csrf_token" }` + `__Host-csrf` cookie; echo in `X-CSRF-Token` on every write (login included) |
| POST | `/auth/login` | public | email + password (+ TOTP for admin) → session |
| POST | `/auth/logout` | any | end session |
| GET | `/auth/google/start` | public | OAuth sign-in |
| GET | `/auth/google/callback` | public | OAuth callback |
| GET | `/invites/:token` | public | validate invite |
| POST | `/invites/:token/accept` | public | set name/password |

### A2. Me
| Method | Path | Purpose |
|---|---|---|
| GET | `/me` | profile + role |
| PATCH | `/me` | name, timezone, quiet hours |
| GET | `/me/unread-count` | `{ "new_events": 3, "notifications": 1 }` |
| POST | `/me/seen` | update `last_seen_at` |
| GET / PUT | `/me/notification-prefs` | offsets, channels, filters |
| GET | `/me/export` | personal data export (JSON) |
| POST | `/me/delete` | delete own account |

### A3. Events
| Method | Path | Purpose |
|---|---|---|
| GET | `/events` | filters: `kind, tier, region, mode, status, deadline_before, deadline_after, registered, q, new_only` |
| GET | `/events/:id` | detail + sources + snippets |
| POST | `/events/:id/registration` | "I registered" |
| DELETE | `/events/:id/registration` | unregister |
| POST / DELETE | `/events/:id/track` | "Remind me" |
| GET | `/events/:id/ics` | single-event .ics |
| GET | `/events/:id/gcal-link` | Google Calendar template URL |

Event object (synthetic example)
```json
{
  "id": "uuid",
  "title": "Example Corp Hackathon 2027",
  "organiser": {"name": "Example Corp", "id": "uuid", "kind": "company", "tier": 1, "region": "india"},
  "registration_url": "https://example.com/hackathon",
  "registration_deadline": "2026-11-20T18:29:00Z",
  "event_start": "2026-12-01T03:30:00Z",
  "event_end": "2026-12-02T12:30:00Z",
  "mode": "online",
  "location": null,
  "eligibility": ["UG", "PG"],
  "status": "open",
  "review_status": "auto",
  "is_new": true,
  "is_registered": false,
  "is_tracked": false,
  "sources": [{"url": "https://example.com/hackathon", "snippet": "Registrations close 20 Nov 2026…", "found_at": "2026-11-13T04:00:00Z"}]
}
```

### A4. Calendar
| Method | Path | Purpose |
|---|---|---|
| GET | `/calendar-items?from=&to=` | user's items |
| POST | `/calendar-items` | create personal event |
| PATCH | `/calendar-items/:id` | drag/resize/edit |
| DELETE | `/calendar-items/:id` | delete |
| GET | `/calendar/feed/:token.ics` | private subscribe feed |
| POST | `/calendar/feed/rotate` | new feed token |

### A5. Notifications and channels
| Method | Path | Purpose |
|---|---|---|
| GET | `/notifications` | in-app list |
| POST | `/notifications/read` | `{ids:[…]}` or `{all:true}` |
| GET | `/channels` | endpoints (addresses masked) |
| POST | `/channels/fcm` | save/refresh FCM token `{token, platform: web\|android, label}` (upsert by token) |
| POST | `/channels/telegram/link` | returns one-time deep link `t.me/<bot>?start=<code>` |
| POST | `/channels/ntfy` | create topic |
| GET | `/channels/google/start` | Calendar OAuth (incremental scope) |
| GET | `/channels/google/callback` | OAuth callback |
| DELETE | `/channels/:id` | remove |
| POST | `/channels/:id/test` | send test message |

### A6. Dashboard (Management, Admin)
| Method | Path | Purpose |
|---|---|---|
| GET | `/dashboard/summary` | counts, closing soon, registrations by user |

### A7. Admin
| Resource | Endpoints |
|---|---|
| Users / invites | `GET/POST /admin/invites`, `GET /admin/users`, `PATCH /admin/users/:id` (role, active) |
| Organisations | CRUD `/admin/organisations`, `POST /admin/organisations/import` (CSV) |
| Sources | CRUD `/admin/sources`, `POST /admin/sources/:id/run`, `GET /admin/sources/health` |
| Review | `GET /admin/review`, `POST /admin/review/:event_id` `{action: approve\|reject\|merge, merge_into?, patch?}` |
| LLM | CRUD `/admin/llm/providers` (key write-only), `POST /admin/llm/providers/:id/test`, `GET /admin/llm/usage?from=&to=` |
| Social | `PUT /admin/social/:network` (cookie upload), `POST /admin/social/:network/check`, `DELETE /admin/social/:network` |
| Audit | `GET /admin/audit` |

### A8. Rate limits
| Scope | Limit |
|---|---|
| `/auth/login` | 5 / 15 min per IP + per email |
| Writes | 60 / min per user |
| `/channels/:id/test` | 5 / hour per user |
| `/admin/sources/:id/run` | 10 / hour |

---

## Part B — External integrations

> Prices, quotas and policy details must be re-checked on the provider's official page before go-live. Items marked **[verify]** are not confirmed.

### B1. Discovery sources
| Source | Method | Notes |
|---|---|---|
| Devpost | Public listing pages **[verify structure/endpoints]** | Respect rate limits |
| Unstop | Public listing pages **[verify]** | |
| MLH | Season events page | Seasonal list |
| RSS / Atom | `feedparser` | Conditional GET |
| Reddit | Official Reddit API (OAuth app) | Follow API terms **[verify current access rules]** |
| Substack | Public `/feed` RSS | |
| X | Logged-in Playwright session | Best-effort |
| LinkedIn, Instagram | Logged-in Playwright session, shared dedicated account | **ToS forbids scraping; account ban possible; best-effort only** |
| Official sites | httpx → selector or LLM; Playwright if JS-rendered | robots.txt respected |

Fetch rules: generic User-Agent `HackEventsBot/1.0` (no personal contact info), per-domain ≤ 1 req / 5 s, retries with backoff (3×), timeout 20 s, body cap 5 MB.

### B2. LLM providers — adapter interface
```python
class LLMProvider(Protocol):
    name: str
    async def extract(self, text: str, schema: dict, *, max_tokens: int) -> LLMResult: ...

@dataclass
class LLMResult:
    data: dict | None          # validated against schema, or None
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    raw: str                   # kept for debugging, never shown to users
```

| Provider | API | Role |
|---|---|---|
| Google Gemini | Gemini API, JSON / response-schema mode | Bulk extraction |
| Anthropic Claude Haiku | Messages API, no tools, JSON output | Verification + fallback |
| OpenAI-compatible | `/v1/chat/completions` with configurable `base_url` | Optional extra provider |

Routing:
1. Cap check (today / month cost from `llm_usage`, cached in Redis) → over cap = skip provider.
2. `extract` provider → validate schema → if invalid or confidence < 0.7 → `verify` provider.
3. Provider error / timeout (30 s) / over cap → next by priority.
4. All fail → `raw_document.status = failed`, retried next cycle; admin alert after 3 consecutive.
5. Gemini-only and Haiku-only modes must both pass the test suite (providers independent).

### B3. Notification channels — plugin interface
```python
class Notifier(Protocol):
    channel: str
    async def send(self, endpoint: ChannelEndpoint, msg: Message) -> SendResult: ...
    async def test(self, endpoint: ChannelEndpoint) -> SendResult: ...
```
`SendResult.permanent_failure=True` (FCM `UNREGISTERED` / `INVALID_ARGUMENT`, Telegram "bot blocked by user") → mark endpoint `is_valid=false`, no retries.

| Channel | Integration | Notes |
|---|---|---|
| Push (FCM) | `firebase-admin` → FCM HTTP v1 `messages:send`; data + notification payload with deep link | Web tokens bound to origin → domain must be final; client re-sends token on each app open |
| Email | Gmail API `users.messages.send` (MIME, HTML + text) from a dedicated Gmail account | Scope `gmail.send` only; sends from Gmail's domain (DuckDNS can't hold SPF/DKIM); daily limit **[verify]**; on token failure alert Admin, fallback Gmail SMTP 587 + app password |
| Telegram | Bot API `sendMessage` | User links via `/start <code>` |
| ntfy | `POST https://ntfy.APP_DOMAIN/<topic>` with access token | Self-hosted, auth on, random topic names |
| Google Calendar | Calendar API v3 `events.insert/patch/delete` with `reminders.overrides` | Scope `calendar.events`; refresh tokens encrypted; OAuth production-status token behaviour **[verify]** |

Message template (all channels)
```
⏱ 2 days left to register
<event title>
Closes 20 Nov 2026, 23:59 IST
https://APP_DOMAIN/events/<id>
```

### B4. Calendar formats
- `.ics`: RFC 5545, `UID = <event_id>@APP_DOMAIN`, `VALARM` per user offset.
- Google template link: `https://calendar.google.com/calendar/render?action=TEMPLATE&text=…&dates=…&details=…`.

### B5. Inbound webhooks
| Path | From | Auth |
|---|---|---|
| `/hooks/telegram` | Telegram | `X-Telegram-Bot-Api-Secret-Token` header |
