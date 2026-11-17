// Notifications centre data: alerts derived from the events API (no inbox endpoint exists yet).
// Three kinds: a tracked event's deadline closing within 3 days, a newly discovered event, and
// a registration confirmation. Read state is per-browser (localStorage).

import { type EventItem, formatDeadline, urgency } from "./events";

export type AlertKind = "deadline" | "new" | "registered";
export type Alert = {
  id: string;
  kind: AlertKind;
  level: "urgent" | "warning" | "info";
  title: string;
  detail: string;
  event: EventItem;
};

export const READ_KEY = "he-alerts-read";
const READ_CAP = 500;
const RANK = { urgent: 0, warning: 1, info: 2 } as const;

/** Alerts for the signed-in user, most urgent first (ties keep API order). */
export function buildAlerts(events: EventItem[], now: Date, tz: string): Alert[] {
  const out: Alert[] = [];
  for (const ev of events) {
    const u = urgency(ev.registration_deadline, now, tz);
    if (ev.is_tracked && !ev.is_registered && (u === "urgent" || u === "warning"))
      out.push({
        // One alert per event per urgency band, so tightening from 3 days to 1 day re-alerts.
        id: `deadline:${ev.id}:${u}`,
        kind: "deadline",
        level: u,
        title: ev.title,
        detail: formatDeadline(ev.registration_deadline, now, tz),
        event: ev,
      });
    if (ev.is_new)
      out.push({ id: `new:${ev.id}`, kind: "new", level: "info", title: ev.title, detail: "New hackathon found", event: ev });
    if (ev.is_registered)
      out.push({
        id: `registered:${ev.id}`,
        kind: "registered",
        level: "info",
        title: ev.title,
        detail: "You are registered",
        event: ev,
      });
  }
  return out.map((a, i) => ({ a, i })).sort((x, y) => RANK[x.a.level] - RANK[y.a.level] || x.i - y.i).map((x) => x.a);
}

/** Alerts that need action (urgent/warning) and were not in the previous refresh: worth a toast.
 * `before` is null on the first load, so opening the page never toasts what was already there. */
export function newActionAlerts(before: Set<string> | null, now: Alert[]): Alert[] {
  return before === null ? [] : now.filter((a) => a.level !== "info" && !before.has(a.id));
}

/** Parse the stored read-set; anything malformed counts as "nothing read". */
export function parseRead(raw: string | null): Set<string> {
  try {
    const v: unknown = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);
  } catch {
    return new Set();
  }
}

/** Serialise, dropping the oldest ids past the cap so storage cannot grow without bound. */
export function serializeRead(read: Set<string>): string {
  return JSON.stringify([...read].slice(-READ_CAP));
}

export const unreadCount = (alerts: Alert[], read: Set<string>) => alerts.filter((a) => !read.has(a.id)).length;
