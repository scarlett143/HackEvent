// Events API types (doc 06 §A2/§A3) + pure feed helpers. Every response shape the web app
// expects from the events API lives here so it can be reconciled with the backend in one place.

export type Kind = "company" | "institution";
export type Region = "india" | "global";
export type Mode = "online" | "offline" | "hybrid" | "unknown";

export type Organiser = {
  name: string;
  id: string | null; // null when the organiser_name did not match a seeded organisation
  kind: Kind | null;
  tier: number | null;
  region: Region | null;
};

export type EventSource = { url: string; snippet: string | null; found_at: string };

/** Item of `GET /events` (doc 06 §A3 event object). Times are ISO 8601 UTC. */
export type EventItem = {
  id: string;
  title: string;
  organiser: Organiser;
  registration_url: string | null;
  registration_deadline: string | null;
  event_start: string | null;
  event_end: string | null;
  mode: Mode;
  location: string | null;
  eligibility: string[];
  status: "open" | "closed" | "cancelled";
  review_status: "auto" | "needs_review" | "approved" | "rejected";
  is_new: boolean;
  is_registered: boolean;
  is_tracked: boolean;
  /** Set when a source moved the deadline (HE-072). */
  deadline_changed_at?: string | null;
};

const DEADLINE_UPDATED_MS = 48 * 3600 * 1000; // App Flow: chip shows for 48 h

/** App Flow edge case: "Deadline updated" chip for 48 h after the deadline moved. */
export function deadlineUpdated(ev: Pick<EventItem, "deadline_changed_at">, now: Date): boolean {
  if (!ev.deadline_changed_at) return false;
  const age = now.getTime() - new Date(ev.deadline_changed_at).getTime();
  return age >= 0 && age < DEADLINE_UPDATED_MS;
}

/** `GET /events/:id`: the list item plus sources/snippets and long-form fields (doc 05 events). */
export type EventDetail = EventItem & {
  description: string | null;
  prize: string | null;
  sources: EventSource[];
};

/** Cursor page (doc 06 conventions). */
export type Page<T> = { items: T[]; next_cursor: string | null };

/** `GET /me/unread-count`. */
export type UnreadCount = { new_events: number; notifications: number };

export const DEFAULT_TZ = "Asia/Kolkata";

/** The feed reloads itself this often (owner decision 2026-11-19; sources crawl hourly). */
export const FEED_REFRESH_MS = 30 * 60_000;

/** True once the list on screen is FEED_REFRESH_MS old (or was never loaded). */
export function feedIsStale(updatedAt: Date | null, now: Date): boolean {
  return updatedAt === null || now.getTime() - updatedAt.getTime() >= FEED_REFRESH_MS;
}
const DAY = 86_400_000;

// ── Deadlines ────────────────────────────────────────────────────────────────

/** Calendar-day difference between two instants as seen in `tz` (0 = same local day). */
export function daysBetween(fromUtc: Date, toUtc: Date, tz: string): number {
  const day = (d: Date) => {
    const [y, m, dd] = new Intl.DateTimeFormat("en-CA", { timeZone: tz }).format(d).split("-");
    return Date.UTC(+y, +m - 1, +dd);
  };
  return Math.round((day(toUtc) - day(fromUtc)) / DAY);
}

const TIME = { hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZoneName: "short" } as const;

/** "15 Nov, 23:59 IST" in the user's timezone (design brief §7). */
export function formatWhen(utc: string, tz: string): string {
  return new Intl.DateTimeFormat("en-IN", { timeZone: tz, day: "numeric", month: "short", ...TIME })
    .format(new Date(utc));
}

/** "13 Nov 2026" — date only, for "found on <date>". */
export function formatDate(utc: string, tz: string): string {
  return new Intl.DateTimeFormat("en-IN", { timeZone: tz, dateStyle: "medium" }).format(new Date(utc));
}

export type Urgency = "urgent" | "warning" | "ok" | "closed" | "none";

/** ≤ 1 day urgent, 2–3 warning, > 3 ok (design brief §3), counted in local calendar days. */
export function urgency(deadlineUtc: string | null, nowUtc: Date, tz: string): Urgency {
  if (!deadlineUtc) return "none";
  const deadline = new Date(deadlineUtc);
  if (deadline <= nowUtc) return "closed";
  const days = daysBetween(nowUtc, deadline, tz);
  return days <= 1 ? "urgent" : days <= 3 ? "warning" : "ok";
}

/** Countdown label: "Closes today 23:59 IST" on day 0, otherwise "2 days left to register". */
export function formatDeadline(deadlineUtc: string | null, nowUtc: Date, tz: string): string {
  if (!deadlineUtc) return "No deadline listed";
  const deadline = new Date(deadlineUtc);
  if (deadline <= nowUtc) return "Registration closed";
  const days = daysBetween(nowUtc, deadline, tz);
  if (days === 0)
    return `Closes today ${new Intl.DateTimeFormat("en-IN", { timeZone: tz, ...TIME }).format(deadline)}`;
  return `${days} ${days === 1 ? "day" : "days"} left to register`;
}

/** Event-date countdown for the detail page. */
export function formatStart(startUtc: string | null, endUtc: string | null, nowUtc: Date, tz: string) {
  if (!startUtc) return "Dates not listed";
  const start = new Date(startUtc);
  if (start > nowUtc) {
    const days = daysBetween(nowUtc, start, tz);
    return days === 0 ? "Starts today" : `Starts in ${days} ${days === 1 ? "day" : "days"}`;
  }
  return endUtc && new Date(endUtc) > nowUtc ? "Happening now" : "Ended";
}

/** Detail-page timeline label: "T-2d 04h" (days+hours), or "T-5h 12m" in the last day. Null once passed. */
export function tMinus(deadlineUtc: string | null, nowUtc: Date): string | null {
  if (!deadlineUtc) return null;
  const ms = new Date(deadlineUtc).getTime() - nowUtc.getTime();
  if (ms <= 0) return null;
  const hours = Math.floor(ms / 3_600_000);
  const days = Math.floor(hours / 24);
  return days > 0
    ? `T-${days}d ${String(hours % 24).padStart(2, "0")}h`
    : `T-${hours}h ${String(Math.floor(ms / 60_000) % 60).padStart(2, "0")}m`;
}

// ── Sections (design brief §4.1) ─────────────────────────────────────────────

/** Pinned "new" first, then deadlines in the next 7 days, then everything else; order kept. */
export function sectionize(items: EventItem[], nowUtc: Date) {
  const week = nowUtc.getTime() + 7 * DAY;
  const fresh: EventItem[] = [];
  const closing: EventItem[] = [];
  const later: EventItem[] = [];
  for (const ev of items) {
    const d = ev.registration_deadline ? new Date(ev.registration_deadline).getTime() : null;
    if (ev.is_new) fresh.push(ev);
    else if (d !== null && d <= week) closing.push(ev);
    else later.push(ev);
  }
  return { fresh, closing, later };
}

// ── Links ────────────────────────────────────────────────────────────────────

/** Only http(s) URLs are rendered as links (AGENTS.md: scraped data is untrusted). */
export function safeHttpUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.href : null;
  } catch {
    return null;
  }
}

// ── Filters ↔ URL (App Flow F3) ──────────────────────────────────────────────

export const FILTER_OPTIONS = {
  kind: ["company", "institution"],
  tier: ["1", "2", "3"],
  region: ["india", "global"],
  mode: ["online", "offline", "hybrid"],
  status: ["open", "closing", "closed"],
  within: ["3", "7", "30"], // deadline window in days → deadline_before
} as const;

type Choice = keyof typeof FILTER_OPTIONS;
export type Filters = { [K in Choice]?: (typeof FILTER_OPTIONS)[K][number] } & {
  q?: string;
  registered?: true;
  new_only?: true;
};

const CHOICES = Object.keys(FILTER_OPTIONS) as Choice[];

/** Read filters from the page URL; unknown keys and values are dropped. */
export function parseFilters(sp: URLSearchParams): Filters {
  const f: Record<string, unknown> = {};
  for (const k of CHOICES) {
    const v = sp.get(k);
    if (v && (FILTER_OPTIONS[k] as readonly string[]).includes(v)) f[k] = v;
  }
  const q = sp.get("q")?.trim().slice(0, 200);
  if (q) f.q = q;
  if (sp.get("registered") === "true") f.registered = true;
  if (sp.get("new_only") === "true") f.new_only = true;
  return f as Filters;
}

/** Canonical page query string (stable key order) for `router.replace`. */
export function filtersToSearch(f: Filters): string {
  const sp = new URLSearchParams();
  if (f.q) sp.set("q", f.q);
  for (const k of CHOICES) if (f[k]) sp.set(k, f[k]);
  if (f.registered) sp.set("registered", "true");
  if (f.new_only) sp.set("new_only", "true");
  return sp.toString();
}

/** `GET /events` query: page filters with the relative window resolved against `nowUtc`. */
export function eventsQuery(f: Filters, nowUtc: Date, cursor: string | null): string {
  const { within, ...rest } = f;
  const sp = new URLSearchParams(filtersToSearch(rest));
  if (within) sp.set("deadline_before", new Date(nowUtc.getTime() + +within * DAY).toISOString());
  if (cursor) sp.set("cursor", cursor);
  sp.set("limit", "50");
  return sp.toString();
}
