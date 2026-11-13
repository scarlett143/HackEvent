# HackEvents — App Flow Document

Version 0.1 · 2026-11-13

---

## 1. Navigation map

```
/login ─► /invite/:token (first time only)
   │
   ▼
/ (Feed) ──┬── /events/:id
           ├── /calendar   (month | week | day | agenda)
           ├── /notifications
           ├── /settings   (profile · timezone · quiet hours · reminders · channels · Google)
           ├── /dashboard  (Management + Admin)
           └── /admin      (Admin only)
                 ├── /admin/sources      (orgs, tiers, sources, health)
                 ├── /admin/review       (needs-review queue)
                 ├── /admin/llm          (providers, keys, caps, usage)
                 ├── /admin/social       (shared sessions)
                 ├── /admin/users        (invites, roles, disable)
                 └── /admin/audit        (audit log)
```

## 2. Flows

### F1 — Invite and first login
1. Admin opens `/admin/users` → "Invite" → email + role.
2. System emails a one-time link (`/invite/:token`, expires 72 h, single use).
3. Invitee opens link → sets name and password (or "Sign in with Google").
4. Onboarding (3 steps, skippable):
   1. Timezone (pre-filled IST).
   2. Channels: enable push (browser permission prompt), connect Telegram, email on by default.
   3. Interests: India/global, online/offline, company/institution.
5. Lands on Feed.

### F2 — Open app → see what's new
1. App loads → `GET /me/unread-count` → badge on bell and "New" chips on cards newer than `last_seen_at`.
2. Feed shows deadline-sorted list; "New since last visit" section pinned on top.
3. After the feed has been visible 3 s, `POST /me/seen` updates `last_seen_at`.

### F3 — Browse and filter
1. Filter bar: type, tier, region, mode, deadline window, status, registered.
2. Filters stored in URL query (shareable) and last-used saved per user.
3. Card tap → `/events/:id`: details, source link, quoted snippet, eligibility, countdown, Register, "I registered", "Add to calendar".

### F4 — Register and track
1. User taps **Register** → opens organiser page in a new tab.
2. Back in app, a toast asks "Did you register?" → **I registered**.
3. `POST /events/:id/registration` → event appears on calendar → reminders recalculated to event-date offsets.
4. Undo available for 10 s; later, "Unregister" in event detail.

### F5 — Track without registering
1. "Remind me" on an open event → deadline reminders (3/2/1/0 days) without registration.

### F6 — Calendar
1. `/calendar` loads registered events + user-created events.
2. Drag/resize → `PATCH /calendar-items/:id` (personal copy; source event is never changed).
3. Click empty slot → create personal event.
4. Export: "Download .ics" or subscribe to personal `.ics` feed URL (secret token).
5. "Add to Google Calendar" per event (template link), or a connected Google account auto-pushes.

### F7 — Reminder delivery
1. Scheduler fires reminder → each enabled channel gets one message.
2. Message: title, "2 days left to register", deadline in user's timezone, deep link `/events/:id`.
3. Tapping push → opens app on that event, marks notification read.

### F8 — Settings
- Reminder offsets (chips: 7, 3, 2, 1, 0 days + custom), separate for deadline and event.
- Per-channel toggles; per-channel test button ("Send test").
- Quiet hours (off by default).
- Connect / disconnect Google Calendar.

### F9 — Management dashboard
1. `/dashboard`: open events count, closing this week, registrations by professor, by organiser type.
2. Weekly digest email every Monday 09:00 IST.

### F10 — Admin: sources and health
1. `/admin/sources`: list orgs with tier, sources, last success, last event found, error streak.
2. Add/edit source: URL, adapter type (rss/api/selector/llm/social), tier override, enabled.
3. "Run now" → queues one scrape, shows result.
4. Red row when 0 results for 5 runs or 3 errors in a row.

### F11 — Admin: review queue
1. `/admin/review` lists `needs_review` events with snippet and extracted fields side by side.
2. Actions: Approve, Edit + approve, Merge with…, Reject.

### F12 — Admin: LLM providers
1. Add provider: type (gemini / anthropic / openai-compatible), base URL, model, key (write-only), role (extract / verify / fallback), daily + monthly caps.
2. "Test" sends a fixed sample page and shows the parsed result.
3. Usage chart: tokens and cost per day per provider.

### F13 — Admin: social sessions
1. `/admin/social` → choose network → upload cookie export from the dedicated account's browser (never a password).
2. System validates the session with one read, stores it encrypted, shows "last checked".
3. Expired session → admin alert, source paused.

## 3. Empty, error, and edge states

| State | Behaviour |
|---|---|
| No events match filters | Message + "Clear filters" |
| Push permission denied | Banner explaining how to re-enable; other channels still work |
| Unsupported browser (e.g. iOS/Safari) | Banner: "Push needs Chrome on Android or desktop"; email/Telegram still work |
| Offline | Cached feed shown read-only, "Offline" chip |
| Event deadline changed | Card shows "Deadline updated" chip for 48 h; reminders recalculated |
| Event cancelled at source | Marked "Cancelled", reminders cancelled, user notified once |
| Session expired | Redirect to `/login`, return to original page after |
| Google token revoked | Settings shows "Reconnect Google"; other channels continue |
