// Calendar screen maths (Stitch calendar / week / day / agenda). Pure: day keys are "YYYY-MM-DD"
// calendar dates in the user's timezone, so grids never depend on the browser's own zone.

import { slots } from "./calendar";
import type { EventItem } from "./events";

export type View = "month" | "week" | "day" | "agenda";
export const VIEWS: View[] = ["month", "week", "day", "agenda"];

export type Entry = {
  key: string;
  eventId: string;
  title: string;
  kind: "deadline" | "event";
  start: Date; // a deadline is a point in time: start === end
  end: Date;
  registered: boolean;
  location: string | null;
};

const MS_DAY = 86_400_000;
const utcMs = (key: string) => {
  const [y, m, d] = key.split("-").map(Number);
  return Date.UTC(y, m - 1, d);
};

/** "2026-11-19" for the instant as seen in `tz`. */
export const dayKeyOf = (d: Date, tz: string) => new Intl.DateTimeFormat("en-CA", { timeZone: tz }).format(d);

export const addDays = (key: string, n: number) => new Date(utcMs(key) + n * MS_DAY).toISOString().slice(0, 10);

/** Weeks start on Sunday (Stitch S M T W T F S). */
export const weekStart = (key: string) => addDays(key, -new Date(utcMs(key)).getUTCDay());
export const weekDays = (key: string) => Array.from({ length: 7 }, (_, i) => addDays(weekStart(key), i));

/** 6 x 7 = 42 day keys covering the month of `key`, padded with neighbours. */
export const monthGrid = (key: string) =>
  Array.from({ length: 42 }, (_, i) => addDays(weekStart(`${key.slice(0, 7)}-01`), i));

/** Move the visible window by one unit of the current view. */
export function step(key: string, view: View, dir: 1 | -1): string {
  if (view === "day") return addDays(key, dir);
  if (view === "week" || view === "agenda") return addDays(key, 7 * dir);
  const [y, m] = key.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1 + dir, 1)).toISOString().slice(0, 10);
}

/** Only events the user tracks or has registered for, as time entries, soonest first. */
export function entriesFor(events: EventItem[]): Entry[] {
  return events
    .filter((e) => e.is_tracked || e.is_registered)
    .flatMap((ev) =>
      slots(ev).map((s): Entry => {
        const deadline = s.uid.endsWith("-deadline");
        const end = new Date(s.end);
        return {
          key: s.uid,
          eventId: ev.id,
          title: ev.title,
          kind: deadline ? "deadline" : "event",
          start: deadline ? end : new Date(s.start),
          end,
          registered: ev.is_registered,
          location: s.location,
        };
      }),
    )
    .sort((a, b) => a.start.getTime() - b.start.getTime());
}

/** Entries that touch the local day `day` (multi-day events appear on every day of their run). */
export const entriesOnDay = (all: Entry[], day: string, tz: string) =>
  all.filter((e) => dayKeyOf(e.start, tz) <= day && day <= dayKeyOf(e.end, tz));

/** Agenda: entries from `fromDay` on, grouped by their first visible day. */
export function agenda(all: Entry[], fromDay: string, tz: string): [string, Entry[]][] {
  const groups = new Map<string, Entry[]>();
  for (const e of all) {
    if (dayKeyOf(e.end, tz) < fromDay) continue;
    const k = dayKeyOf(e.start, tz) < fromDay ? fromDay : dayKeyOf(e.start, tz);
    groups.set(k, [...(groups.get(k) ?? []), e]);
  }
  return [...groups].sort(([a], [b]) => (a < b ? -1 : 1));
}

export const monthLabel = (key: string) =>
  new Intl.DateTimeFormat("en-IN", { timeZone: "UTC", month: "long", year: "numeric" }).format(new Date(utcMs(key)));
export const dayLabel = (key: string) =>
  new Intl.DateTimeFormat("en-IN", { timeZone: "UTC", weekday: "long", day: "numeric", month: "short" }).format(
    new Date(utcMs(key)),
  );
export const timeLabel = (d: Date, tz: string) =>
  new Intl.DateTimeFormat("en-IN", { timeZone: tz, hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(d);
