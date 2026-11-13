# HackEvents — Backend Schema Document

Version 0.1 · 2026-11-13 · PostgreSQL 16

Conventions: `id uuid PK default gen_random_uuid()`, `created_at` / `updated_at timestamptz default now()` on every table (omitted below). All times stored in **UTC**; display converts to user timezone. Extensions: `pgcrypto`, `pg_trgm`, `citext`.

---

## 1. ER overview

```
users ─┬─< sessions
       ├─< invites (created_by)
       ├─< notification_prefs (1:1)
       ├─< channel_endpoints (fcm tokens, telegram chat, email, ntfy topic, gcal)
       ├─< registrations >── events ─┬─< event_sources >── raw_documents >── sources >── organisations
       ├─< tracks         >── events  │
       ├─< calendar_items             └─< reminders >── users
       ├─< notifications
       └─< audit_log
llm_providers ─< llm_usage
llm_cache
social_sessions (one per network)
```

## 2. Tables

### users
| Column | Type | Notes |
|---|---|---|
| email | citext unique not null | |
| name | text not null | |
| role | enum `admin` \| `management` \| `professor` | |
| password_hash | text null | Argon2id; null if Google-only |
| totp_secret_enc | bytea null | required for admin |
| google_sub | text unique null | |
| timezone | text not null default `'Asia/Kolkata'` | IANA name |
| quiet_start, quiet_end | time null | null = off |
| last_seen_at | timestamptz null | "New since last visit" |
| is_active | bool default true | |

### sessions
| Column | Type | Notes |
|---|---|---|
| user_id | uuid FK users | |
| token_hash | text unique | sha256 of cookie value |
| expires_at | timestamptz | |
| ip, user_agent | text | |

### invites
| Column | Type | Notes |
|---|---|---|
| email | citext | |
| role | enum | |
| token_hash | text unique | |
| expires_at | timestamptz | 72 h |
| used_at | timestamptz null | single use |
| created_by | uuid FK users | |

### organisations
| Column | Type | Notes |
|---|---|---|
| name | text not null | |
| kind | enum `company` \| `institution` | |
| region | enum `india` \| `global` | |
| tier | smallint check 1–3 | |
| rank_source | text | e.g. `NIRF`, `QS`, `Fortune-G500` |
| website | text | |
| logo_url | text null | |
| is_active | bool | |

Unique `(lower(name), kind)`.

### sources
| Column | Type | Notes |
|---|---|---|
| organisation_id | uuid FK null | null for aggregators |
| kind | enum `official` \| `aggregator` \| `rss` \| `reddit` \| `substack` \| `social` | |
| network | enum null `linkedin` \| `instagram` \| `x` | social only |
| url | text not null | |
| adapter | enum `rss` \| `api` \| `selector` \| `llm` | |
| selector_config | jsonb null | |
| tier_override | smallint null | |
| cadence_hours | int not null | derived from tier, editable |
| next_run_at | timestamptz | indexed |
| last_success_at, last_event_at | timestamptz null | |
| error_streak, empty_streak | int default 0 | health |
| last_error | text null | |
| etag, last_modified, content_hash | text null | change detection |
| is_enabled | bool | |

### raw_documents
| Column | Type | Notes |
|---|---|---|
| source_id | uuid FK | |
| fetched_at | timestamptz | |
| url | text | |
| content_hash | text | indexed |
| body | text | truncated to 200 KB |
| status | enum `new` \| `parsed` \| `failed` \| `skipped` | |

Retention: 30 days, then deleted by a scheduled job.

### events
| Column | Type | Notes |
|---|---|---|
| title | text not null | |
| organiser_name | text not null | as published |
| organisation_id | uuid FK null | matched org |
| description | text | |
| registration_url | text | |
| registration_deadline | timestamptz null | |
| event_start, event_end | timestamptz null | |
| mode | enum `online` \| `offline` \| `hybrid` \| `unknown` | |
| location | text null | |
| eligibility | text[] | chips |
| prize | text null | |
| status | enum `open` \| `closed` \| `cancelled` | |
| review_status | enum `auto` \| `needs_review` \| `approved` \| `rejected` | |
| confidence | numeric(3,2) | |
| dedupe_key | text unique | see TRD §4.3 |
| first_seen_at | timestamptz | drives "New" |
| deadline_changed_at | timestamptz null | |

Indexes: `(status, registration_deadline)`, `(first_seen_at desc)`, GIN trigram on `title`.

### event_sources
| Column | Type | Notes |
|---|---|---|
| event_id | uuid FK | |
| raw_document_id | uuid FK null | |
| source_url | text | |
| snippet | text | quoted evidence |
| extracted | jsonb | raw extracted record |
| extractor | text | `rss` / `devpost` / `gemini:<model>` / … |

Unique `(event_id, source_url)`.

### registrations
| Column | Type | Notes |
|---|---|---|
| user_id | uuid FK | |
| event_id | uuid FK | |
| registered_at | timestamptz | |

Unique `(user_id, event_id)`.

### tracks ("Remind me" without registering)
Same shape as `registrations`; unique `(user_id, event_id)`.

### calendar_items
| Column | Type | Notes |
|---|---|---|
| user_id | uuid FK | |
| event_id | uuid FK null | null = personal event |
| title | text | |
| starts_at, ends_at | timestamptz | user's own copy; drag/resize edits this |
| all_day | bool | |
| color | text null | |
| gcal_event_id | text null | when pushed to Google |

### calendar_feeds
| Column | Type | Notes |
|---|---|---|
| user_id | uuid FK unique | |
| token_hash | text unique | private .ics feed, rotatable |

### notification_prefs
| Column | Type | Notes |
|---|---|---|
| user_id | uuid FK unique | |
| deadline_offsets_days | int[] default `{3,2,1,0}` | |
| event_offsets_days | int[] default `{3,2,1,0}` | |
| send_hour_local | smallint default 9 | reminders fire at this local hour |
| new_event_alerts | bool default true | |
| new_event_filters | jsonb | region / kind / mode |
| channels | jsonb | `{"fcm":true,"email":true,"telegram":false,...}` |

### channel_endpoints
| Column | Type | Notes |
|---|---|---|
| user_id | uuid FK | |
| channel | enum `fcm` \| `email` \| `telegram` \| `ntfy` \| `gcal` | |
| platform | enum null `web` \| `android` | FCM only; `android` = future native app |
| address_enc | bytea | encrypted (FCM token, chat id, OAuth refresh token) |
| label | text | "Chrome on laptop" |
| is_valid | bool | false after FCM `UNREGISTERED` / `INVALID_ARGUMENT` or revoked |
| token_refreshed_at | timestamptz null | FCM token last re-sent by client |
| last_ok_at | timestamptz null | |

### reminders
| Column | Type | Notes |
|---|---|---|
| user_id, event_id | uuid FK | |
| kind | enum `deadline` \| `event` | |
| offset_days | smallint | |
| channel | enum (as above) | |
| scheduled_at | timestamptz | partial index where `status='pending'` |
| status | enum `pending` \| `sent` \| `failed` \| `cancelled` | |
| attempts | smallint default 0 | |
| sent_at | timestamptz null | |
| last_error | text null | |

**Unique `(user_id, event_id, kind, offset_days, channel)`** → each reminder is sent once.

### notifications (in-app)
| Column | Type | Notes |
|---|---|---|
| user_id | uuid FK | |
| type | text | `new_event`, `reminder`, `deadline_changed`, `cancelled` |
| title, body | text | |
| event_id | uuid FK null | |
| read_at | timestamptz null | |

### llm_providers
| Column | Type | Notes |
|---|---|---|
| name | text | |
| type | enum `gemini` \| `anthropic` \| `openai_compatible` | |
| base_url | text null | |
| model | text | |
| api_key_enc | bytea | write-only from UI |
| role | enum `extract` \| `verify` \| `fallback` | |
| priority | smallint | |
| input_price_per_mtok, output_price_per_mtok | numeric(10,4) | admin-entered, for cost calc |
| daily_cap_usd, monthly_cap_usd | numeric(10,2) | |
| is_enabled | bool | |

### llm_usage
| Column | Type | Notes |
|---|---|---|
| provider_id | uuid FK | |
| job_id | text | |
| input_tokens, output_tokens | int | |
| cost_usd | numeric(10,5) | |
| cache_hit | bool | |
| ok | bool | |
| at | timestamptz | indexed |

### llm_cache
| Column | Type | Notes |
|---|---|---|
| content_hash | text | PK with schema_version |
| schema_version | int | |
| result | jsonb | |
| provider_id | uuid FK | |

### social_sessions
| Column | Type | Notes |
|---|---|---|
| network | enum unique | |
| cookies_enc | bytea | |
| account_label | text | |
| status | enum `valid` \| `expired` \| `banned` | |
| last_checked_at | timestamptz | |

### audit_log
| Column | Type | Notes |
|---|---|---|
| actor_id | uuid FK null | |
| action | text | |
| target_type | text | |
| target_id | uuid null | |
| meta | jsonb | never secrets |
| ip | text | |
| at | timestamptz | |

Append-only: app DB role has INSERT + SELECT only.

## 3. Migrations

- Alembic, one migration per ticket, reviewed.
- Never edit an applied migration; add a new one.
- Destructive changes in two steps (add new → backfill → drop old in a later release).

## 4. Seed data

`seeds/companies.csv`, `seeds/institutions.csv`, `seeds/sources.csv` with columns `name, kind, region, tier, rank_source, website`. Seed script is idempotent (upsert by `(lower(name), kind)`).
