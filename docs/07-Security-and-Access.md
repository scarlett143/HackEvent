# HackEvents — Security & Access Document

Version 0.1 · 2026-11-13

---

## 1. Assets

| Asset | Sensitivity |
|---|---|
| LLM API keys | High (direct cost) |
| Social session cookies | High (account takeover, ban) |
| Google OAuth refresh tokens | High (user calendar write access) |
| User emails, Telegram chat IDs, FCM tokens | Medium (personal data) |
| Push subscriptions | Medium |
| Event data | Low (public) |

## 2. Trust boundaries

1. **Browser ↔ API** — browser never receives keys, cookies, tokens, or other users' data.
2. **Scraped content ↔ system** — all scraped text is **untrusted data**, never instructions (doc 08).
3. **API ↔ workers** — internal Docker network only.
4. **System ↔ external providers** — outbound only, TLS, keys from encrypted store.
5. **FCM** — outbound only to Google; service-account JSON never leaves the server.

## 3. Roles and permissions

| Capability | Professor | Management | Admin |
|---|:-:|:-:|:-:|
| View feed, event detail | ✓ | ✓ | ✓ |
| Register / track / own calendar | ✓ | ✓ | ✓ |
| Own settings and channels | ✓ | ✓ | ✓ |
| College dashboard + digest | – | ✓ | ✓ |
| See other users' registrations | – | ✓ | ✓ |
| Review queue | – | – | ✓ |
| Sources, tiers, orgs | – | – | ✓ |
| LLM providers, keys, caps | – | – | ✓ |
| Social sessions | – | – | ✓ |
| Users, invites, roles | – | – | ✓ |
| Audit log | – | – | ✓ |

Enforcement
- Role check in a FastAPI dependency on every router (`require_role("admin")`).
- **Row ownership check** on every user-owned resource (`calendar_items`, `channel_endpoints`, `registrations`, `tracks`, `notifications`): every query filters `user_id = current_user.id`; other users' rows return 404. Tests cover cross-user access for each endpoint.
- Deny by default: a route without an explicit role dependency fails a CI check.

## 4. Authentication

- Invite-only; invite tokens random 32 bytes, stored hashed, single use, 72 h expiry.
- Passwords: Argon2id, min 12 chars, checked against a breached-password list.
- Optional Google sign-in (OIDC), must match the invited email.
- Sessions: random 32-byte token in `HttpOnly; Secure; SameSite=Lax` cookie, stored hashed, 14-day sliding expiry, revoked on logout / password change / deactivation.
- Admin: **TOTP 2FA required**.
- Login rate limit + lockout (5 fails / 15 min).
- CSRF: double-submit token on all state-changing requests (incl. login), bound to the session; `Origin`, when sent, must be `https://APP_DOMAIN` (HE-015, `services/api/csrf.py`).

## 5. Secrets

| Secret | Storage |
|---|---|
| `MASTER_KEY`, `SESSION_SECRET`, Firebase service-account JSON, Gmail sender refresh token, DuckDNS token, provider tokens | `.env` on server, `chmod 600`, owned by deploy user, **never in git** |
| LLM keys, social cookies, OAuth refresh tokens, FCM tokens, chat IDs, TOTP secrets | DB columns encrypted with AES-256-GCM (envelope: per-row data key wrapped by `MASTER_KEY`) |

Rules
- Keys are **write-only** in the UI — shown masked (last 4 chars) after saving; API never returns them.
- **Never log secrets.** Logging filter redacts known key patterns and `Authorization`, `Cookie`, `Set-Cookie` headers.
- Never ask a user for a social password. Social access = Admin uploads a cookie export from a **dedicated account**.
- `MASTER_KEY` rotation via re-wrap script (Runbook doc 10).
- `MASTER_KEY` backed up separately from DB backups (a stolen dump alone is useless).

## 6. Transport and headers

- TLS via Caddy (Let's Encrypt), HTTP → HTTPS redirect, HSTS (`max-age=31536000`) once the domain is final.
- Headers: CSP (`default-src 'self'`, nonce-based scripts; `connect-src` allows the Firebase/FCM endpoints **[verify exact hosts]**), `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, minimal `Permissions-Policy`, `frame-ancestors 'none'`.
- CORS: same origin only.

## 7. Network

- Oracle security list + host firewall: inbound **80, 443** public; **22 from admin IP only** (or Oracle Bastion). Everything else closed.
- SSH: key-only, no root login, fail2ban.
- Postgres and Redis on the Docker network only; no published ports.
- Outbound allowed (scraping, providers).

## 8. Scraper safety

- **SSRF guard**: admin-entered source URLs resolved and rejected if they point to private, loopback, link-local, or metadata ranges (`169.254.0.0/16`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `127.0.0.0/8`, `::1`, `fc00::/7`, `fe80::/10`). Re-checked after each redirect.
- Playwright runs in its own container, non-root, no DB credentials, downloads disabled.
- Body size cap 5 MB, timeout 20 s.
- Scraped HTML is **never** rendered as HTML in the UI — text only; links restricted to `http/https` with `rel="noopener noreferrer"`.

## 9. Social account policy

- One dedicated account per network, created for this purpose — not a staff member's personal account.
- Low rate (tier cadence only), read-only actions, no posting, no messaging.
- LinkedIn/Instagram are best-effort; ToS risk acknowledged by the owner. If banned: pause the source; do not build evasion workarounds.

## 10. Data protection

- Personal data kept minimal: name, email, optional Telegram chat ID.
- India DPDP Act 2023: purpose = notifications; users can export and delete their data (`/me/export`, `/me/delete`) **[confirm applicability with the college]**.
- Deleting a user removes endpoints, prefs, reminders, calendar items; audit entries keep `actor_id` nulled.
- Raw scraped documents deleted after 30 days.

## 11. Audit log

Logged: login success/fail, invite create/accept, role change, user deactivate, provider add/edit/delete, cap change, social session upload/delete, source create/edit/delete, review decisions, backup restore. Never stores secret values.

## 12. Dependencies and supply chain

- Lockfiles committed (`uv.lock`, `pnpm-lock.yaml`).
- `pip-audit` and `pnpm audit` in CI; Renovate/Dependabot weekly.
- Docker images pinned by digest; Trivy scan in CI.
- No `curl | sh` installs on the server.

## 13. Incident response

1. Suspected key leak → revoke at provider, rotate in Admin portal, review `llm_usage`.
2. Suspected server compromise → snapshot disk, rotate `MASTER_KEY`, all provider keys, Firebase service-account key (revoke in Google Cloud IAM), Telegram token, Google client secret; restore from backup to a fresh VM.
3. Social account banned → mark session `banned`, pause its sources, inform owner.
