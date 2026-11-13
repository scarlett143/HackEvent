# HackEvents — Deployment & Testing Runbook

Version 0.1 · 2026-11-13 · Target: Oracle Cloud single VM, Docker Compose

> Do **not** deploy to any shared production server (separate VM only).
> Verify Oracle shape limits, PAYG behaviour, and provider prices on official pages at provisioning time (TRD §8).

---

## Part A — Environments

| Env | Where | Domain | Data |
|---|---|---|---|
| Local | Developer Mac, Docker Desktop | `localhost` | Seed + fixtures |
| Staging | Same VM, Compose project `he-staging` (optional; add when needed) | temp subdomain | Anonymised copy |
| Production | Oracle VM | `<name>.duckdns.org` | Real |

## Part B — First-time provisioning

### B1. Oracle account
1. Upgrade tenancy to **Pay As You Go**.
2. Create a **budget alert** (small monthly amount; alert at 50% and 100%).
3. Region: close to India (Mumbai or Hyderabad) **[verify Ampere A1 capacity]**.

### B2. VM
1. Shape: Ampere A1 Flex, sized within the free allocation **[verify current limits in console]**.
2. Image: Ubuntu 24.04 LTS (arm64).
3. Boot volume: 100 GB.
4. Add your SSH public key; no password auth.
5. Security list ingress: TCP 80, 443 from `0.0.0.0/0`; TCP 22 from your IP only.

### B3. Host hardening
```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install ufw fail2ban unattended-upgrades
sudo adduser --disabled-password deploy && sudo usermod -aG sudo deploy
# copy authorized_keys to deploy, then in /etc/ssh/sshd_config:
#   PermitRootLogin no
#   PasswordAuthentication no
sudo systemctl restart ssh
sudo ufw default deny incoming
sudo ufw allow 22/tcp && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp
sudo ufw enable
sudo dpkg-reconfigure -plow unattended-upgrades
```
Oracle Ubuntu images also ship their own iptables rules — confirm 80/443 are open in both iptables and the security list (`sudo iptables -L INPUT -n`).

### B4. Docker
Install Docker Engine + Compose plugin from Docker's official apt repository (not the convenience script). Add `deploy` to the `docker` group.

### B5. App
```bash
sudo -iu deploy
git clone <repo-url> ~/hackevents && cd ~/hackevents/infra
cp .env.example .env && chmod 600 .env
# fill .env: APP_DOMAIN, MASTER_KEY (openssl rand -base64 32), SESSION_SECRET,
#            Firebase config + FCM VAPID key, email provider, Telegram token, Google client
# copy Firebase service-account JSON to ~/hackevents/secrets/firebase.json (chmod 600, never in git)
docker compose pull && docker compose up -d
docker compose exec api alembic upgrade head
docker compose exec api python -m seeds.load
docker compose exec api python -m scripts.create_admin --email <admin-email>
```
Store `MASTER_KEY` in a password manager **separately** from backups.

### B6. Domain (DuckDNS)
1. Sign in at duckdns.org with the **admin's dedicated Google account**, claim `<name>`, set its IP to the VM public IP (reserve a **static public IP** in Oracle first).
2. `ntfy.<name>.duckdns.org` resolves automatically (sub-subdomains follow the main record); Caddy gets its own certificate for it.
3. Safety net if the IP ever changes: cron every 5 min
   `curl -s "https://www.duckdns.org/update?domains=<name>&token=$DUCKDNS_TOKEN&ip="` (token from `.env`, never in the crontab line itself — read it from the file).
4. Google Search Console → add property `https://<name>.duckdns.org` → **HTML file** verification (serve the file via Caddy). The single DuckDNS TXT value stays free.

### B7. Email (Gmail API)
1. Create a dedicated Gmail account for sending (`<sender>@gmail.com`), 2-step verification on.
2. In the same Google Cloud project as Calendar OAuth: enable **Gmail API**, add scope `gmail.send`, set consent screen to **In production**.
3. Run `docker compose exec api python -m scripts.gmail_authorize` once, sign in as the sender, store the refresh token in `.env` (`GMAIL_OAUTH_REFRESH_TOKEN`).
4. Send a test email to a Gmail and a college address; confirm Inbox, not spam.
5. Fallback if the token keeps expiring: Gmail SMTP `smtp.gmail.com:587` with an app password.

**Firebase (FCM):** create a Firebase project → add a Web app → Cloud Messaging → generate Web Push certificate key pair → create a service account with the Firebase Messaging role → add `APP_DOMAIN` to authorised domains. (Native Android app later: register the Android app in the same Firebase project.)

Then in Google Cloud Console → OAuth consent screen: add `APP_DOMAIN` as an authorised domain; redirect URIs `https://APP_DOMAIN/api/v1/auth/google/callback` and `https://APP_DOMAIN/api/v1/channels/google/callback`.

## Part C — Routine deploy

```bash
cd ~/hackevents && git pull
cd infra && docker compose pull && docker compose build
./backup.sh                                   # fresh dump before every deploy
docker compose run --rm api alembic upgrade head   # migrations are backward-compatible
docker compose up -d
./smoke.sh
```
Rollback: `git checkout <previous-tag> && docker compose up -d --build` (two-step migrations keep old code working).

Tag every release: `git tag vYYYY.MM.DD-N`.

## Part D — Smoke test (`infra/smoke.sh`)

| Check | Pass |
|---|---|
| `GET https://APP_DOMAIN/` | 200 |
| `GET /api/v1/health` | `{"db":"ok","redis":"ok","workers":"ok"}` |
| TLS certificate | Valid, > 14 days left |
| `docker compose ps` | All `healthy` |
| Scheduler heartbeat in Redis | < 2 min old |
| Test push to Admin | Received |

## Part E — Backups

| Item | Schedule | Target | Retention |
|---|---|---|---|
| `pg_dump -Fc` | Nightly 02:00 IST | Oracle Object Storage | 14 daily, 8 weekly |
| Same dump | Nightly | Off-Oracle (e.g. Backblaze B2 or Google Drive via rclone) | 14 daily |
| `.env` | On change | Password manager | – |

**Restore drill (monthly):**
```bash
docker compose -p he-restore up -d postgres
docker compose -p he-restore exec -T postgres pg_restore -U hackevents -d hackevents --clean < latest.dump
docker compose -p he-restore run --rm api python -m scripts.verify_restore   # compares users/events/reminders counts
```
Record date and result in `docs/ops-log.md`.

## Part F — Monitoring and alerts

| Signal | Threshold | Alert to |
|---|---|---|
| Uptime (external pinger, e.g. UptimeRobot free) | 2 failed checks | Admin Telegram + email |
| Disk usage | > 80% | Admin |
| Container restart loop | > 3 in 10 min | Admin |
| Source health | 5 empty or 3 error runs | Admin (daily summary) |
| LLM spend | 80% of daily / monthly cap | Admin |
| Reminder lag | Pending reminders > 10 min past due | Admin |
| Social session | Expired / banned | Admin |
| Backup | Job failed or missing | Admin |

External uptime pings are for **monitoring only** — they do not prevent Oracle idle reclamation (PAYG handles that).

## Part G — Testing strategy

### G1. Test layers
| Layer | Tool | Scope | Target |
|---|---|---|---|
| Unit | pytest, Vitest | Normalise, dedupe key, reminder planner, tz, caps, SSRF guard, crypto | ≥ 80% on `services/common` |
| Adapter | pytest + saved HTML/JSON fixtures | Each source adapter | ≥ 1 fixture each |
| LLM golden set | pytest marker `golden` | 50 pages × each provider | Gates in doc 08 §A9 |
| API | pytest + httpx AsyncClient + test Postgres | Every endpoint: role, ownership, validation | 100% of endpoints |
| E2E | Playwright | App flows F1–F8 | Critical flows green |
| Accessibility | axe-core in Playwright | Key screens | 0 serious |
| Load (light) | k6 or Locust | 10 users, feed + calendar | p95 < 300 ms |
| Security | ZAP baseline, pip-audit, pnpm audit, Trivy | CI + pre-release | 0 high |

### G2. Must-pass test cases
| # | Case | Expected |
|---|---|---|
| T1 | Same hackathon from Devpost + official site | One event, two sources |
| T2 | Deadline moved from 20 Nov to 25 Nov | Pending reminders recalculated; "Deadline updated" chip |
| T3 | Reminder job runs twice concurrently | Exactly one send per channel |
| T4 | User in `America/New_York` | Reminder at 09:00 local |
| T5 | Quiet hours 22:00–07:00, reminder due 23:00 | Sent 07:00 |
| T6 | Professor requests another user's calendar item | 404 |
| T7 | Professor calls `/admin/*` | 403 |
| T8 | Source URL `http://169.254.169.254/` | Rejected |
| T9 | Injection pages (doc 08 §A10) | No instruction followed |
| T10 | Gemini disabled | Haiku handles extraction; suite passes |
| T11 | Haiku disabled | Gemini extracts; low-confidence → needs_review; suite passes |
| T12 | Daily cap reached | Fallback used; then queue paused + alert |
| T13 | FCM returns `UNREGISTERED` for a token | Endpoint marked invalid; no retries; email still sent |
| T14 | Google token revoked | Reconnect banner; other channels fine |
| T15 | Admin reads LLM provider via API | Key masked |
| T16 | Grep logs after a full run for key/cookie patterns | 0 hits |
| T17 | Android Chrome installed PWA, app closed, phone locked | Push received; tap opens event |
| T18 | Restore backup on a clean VM | App works, counts match |

### G3. Release checklist
- [ ] CI green (lint, types, tests, golden set, audits)
- [ ] Migrations reviewed (backward-compatible)
- [ ] Backup taken immediately before deploy
- [ ] Smoke test passes on prod
- [ ] Changelog entry
- [ ] Admin notified

## Part H — Incident playbooks

| Incident | Steps |
|---|---|
| Site down | `docker compose ps` → logs of unhealthy service → restart → if host issue, use Oracle serial console |
| Disk full | `docker image prune -af`, purge `raw_documents` older than 30 days, check Postgres size |
| TLS failing | `docker compose logs caddy`; check DNS A record and ports 80/443 |
| Emails to spam / not sent | Check `worker-notify` logs; re-run `scripts.gmail_authorize` if token revoked; check sender account for Google security holds; ask recipients to mark "Not spam" once |
| VM lost | New VM → Part B → restore latest dump → update DNS A record → smoke |
| Key leaked | Security doc §13 |

## Part I — Domain change (only if moving off DuckDNS, e.g. to a college subdomain)

1. Point the new domain's A record to the VM; confirm Caddy issues a certificate.
2. Set `APP_DOMAIN` in `.env`, `docker compose up -d`.
3. Update Google OAuth authorised domain + redirect URIs, Firebase authorised domains, Telegram webhook URL. (Email is unaffected — it sends from Gmail.)
4. Users must **re-subscribe to push** and **reconnect Google** if the domain changes after they subscribed — so cut over **before** real users onboard.
