# HackEvents — UI/UX Design Brief

Version 0.1 · 2026-11-13

---

## 1. Design goals

1. **Never miss a deadline** — urgency is the first thing the eye reads.
2. **Scan fast** — a professor checks the app for 30 seconds between classes.
3. **Trust** — every event shows where it came from.
4. **Calm** — light, quiet UI; colour only for meaning.

## 2. Principles

- Light mode default; dark mode available in settings.
- Mobile-first layouts; desktop gets more columns, not different features.
- One primary action per screen.
- No dark patterns, no auto-play, no repeated notification nags.

## 3. Visual system

### Colour (light mode)

| Token | Use | Value (start) |
|---|---|---|
| `bg` | Page | `#FFFFFF` |
| `surface` | Cards | `#F8FAFC` |
| `border` | Dividers | `#E2E8F0` |
| `text` | Body | `#0F172A` |
| `text-muted` | Meta | `#475569` |
| `primary` | Buttons, links | `#2563EB` |
| `urgent` | ≤ 1 day left | `#DC2626` |
| `warning` | 2–3 days left | `#D97706` |
| `ok` | > 3 days / registered | `#16A34A` |
| `new` | "New" badge | `#7C3AED` |

Status colour is **always paired with a text label or icon** (never colour alone). Final values must pass the contrast check in §6.

### Type
- Font: Inter (or system UI stack).
- Scale: 12 / 14 / 16 / 20 / 24 / 32. Body 16 on mobile.
- Countdown numbers use tabular figures.

### Spacing and shape
- 4 px grid; card padding 16; radius 12; one soft shadow level.

### Icons
- Lucide icon set (ships with shadcn/ui).

## 4. Key screens

### 4.1 Feed
```
┌───────────────────────────────────────────┐
│ HackEvents          🔔3   [avatar]        │
├───────────────────────────────────────────┤
│ [Search…]  [Filters ▾]  [India|Global]    │
│ ── New since last visit (3) ───────────── │
│ ┌───────────────────────────────────────┐ │
│ │ NEW  Example Corp Hackathon 2027      │ │
│ │ Example Corp · Company · Tier 1 · Online│
│ │ ⏱ 2 days left to register (warning)   │ │
│ │ [UG] [PG] [Team 2-4] [Free]           │ │
│ │ [Register ↗]   [I registered]  [☆]    │ │
│ └───────────────────────────────────────┘ │
│ ── Closing this week ─────────────────── │
│ ── Later ──────────────────────────────── │
└───────────────────────────────────────────┘
 [Feed] [Calendar] [Alerts] [Settings]   ← bottom nav (mobile)
```
- Desktop: left sidebar nav, feed centre, event preview pane on the right.
- Sort: deadline ascending; closed events hidden by default.

### 4.2 Event detail
- Title, organiser with logo, type/tier chips.
- Two clear dates: **Registration closes** and **Event dates**, each with countdown.
- Eligibility, mode, location, prizes (if extracted).
- **Source** block: link + quoted snippet + "found on <date> via <source>".
- "Needs review" banner if unverified.
- Actions: Register ↗, I registered, Remind me, Add to calendar ▾ (Google / .ics).

### 4.3 Calendar
- Views: Month, Week, Day, Agenda; "Today" button; mini-month in desktop sidebar.
- Colour by type: registered event (primary), deadline marker (warning), personal (grey).
- Drag, resize, click-to-create, keyboard navigation.
- Follows familiar calendar conventions with HackEvents' own styling — not a pixel copy of Google Calendar.

### 4.4 Notifications centre
- List grouped by Today / Earlier; unread dot; "Mark all read".

### 4.5 Settings
- Sections: Profile · Timezone · Reminders · Channels · Quiet hours · Google · Appearance.
- Reminder offsets as toggle chips with "+ custom".
- Each channel: status, toggle, "Send test".

### 4.6 Admin
- Dense tables (TanStack Table): sortable, filterable.
- Source health: green/amber/red row with reason text.
- LLM page: provider cards with cap progress bars.

## 5. Interaction details

| Element | Behaviour |
|---|---|
| "I registered" | Optimistic update, 10 s undo toast |
| Push permission | Ask only after the user taps "Enable push", never on first load |
| Countdown | Updates every minute; "Closes today 23:59 IST" on day 0 |
| Pull to refresh | On mobile feed |
| Skeletons | For feed and calendar loads > 200 ms |
| Toasts | Bottom on mobile, top-right on desktop |

## 6. Accessibility (WCAG 2.2 AA)

- Contrast ≥ 4.5:1 text, 3:1 UI components.
- Full keyboard support, visible focus ring.
- Touch targets ≥ 44 × 44 px.
- `aria-live` for toasts and countdown day changes.
- Calendar has an agenda (list) view as the accessible alternative.
- Respects `prefers-reduced-motion`.

## 7. Copy tone

- Short, direct, no exclamation marks.
- Deadlines in user's timezone with label: "Closes 15 Nov, 23:59 IST".
- Reminder text pattern: "**2 days left** to register for <event title>".

## 8. PWA

- Manifest: name "HackEvents", standalone display, theme colour `primary`.
- Icons: 192, 512, maskable.
- Install prompt shown after the second visit, dismissible.
- Offline: app shell + last feed cached.

## 9. Deliverables before build

1. Low-fi wireframes for the 6 key screens (mobile + desktop).
2. Token file (Tailwind config) from §3.
3. Clickable prototype of F2 and F4 (App Flow doc).
