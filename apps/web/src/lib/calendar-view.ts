// Calendar screen maths (Stitch calendar / week / day / agenda). Pure: day keys are "YYYY-MM-DD"
// calendar dates in the user's timezone, so grids never depend on the browser's own zone.

import { slots } from "./calendar";
import type { EventItem } from "./events";

export type View = "month" | "week" | "day" | "agenda";
export const VIEWS: View[] = ["month", "week", "day", "agenda"];

/** A row of GET /calendar-items (HE-060): a personal item, or the user's copy of a registered event's run. */
export type CalendarItem = {
  id: string;
  event_id: string | null;
  title: string;
  starts_at: string;
  ends_at: string;
  all_day: boolean;
  color: string | null;
};

export type Entry = {
  key: string;
  itemId: string | null; // set = a calendar item: draggable, editable
  eventId: string | null; // null = personal item
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

/** Tracked or registered events (deadlines + runs) plus the user's calendar items, soonest first.
 *  An event's run comes from its calendar copy when there is one (that copy is what drag edits). */
export function entriesFor(events: EventItem[], items: CalendarItem[] = []): Entry[] {
  const copied = new Set(items.map((i) => i.event_id));
  const byId = new Map(events.map((e) => [e.id, e]));
  const fromEvents = events
    .filter((e) => e.is_tracked || e.is_registered)
    .flatMap((ev) =>
      slots(ev)
        .filter((s) => !(s.uid.endsWith("-event") && copied.has(ev.id)))
        .map((s): Entry => {
          const deadline = s.uid.endsWith("-deadline");
          const end = new Date(s.end);
          return {
            key: s.uid,
            itemId: null,
            eventId: ev.id,
            title: ev.title,
            kind: deadline ? "deadline" : "event",
            start: deadline ? end : new Date(s.start),
            end,
            registered: ev.is_registered,
            location: s.location,
          };
        }),
    );
  const fromItems = items.map(
    (i): Entry => ({
      key: `item-${i.id}`,
      itemId: i.id,
      eventId: i.event_id,
      title: i.title,
      kind: "event",
      start: new Date(i.starts_at),
      end: new Date(i.ends_at),
      registered: i.event_id !== null, // copies are made on registration (HE-060)
      location: (i.event_id && byId.get(i.event_id)?.location) || null,
    }),
  );
  return [...fromEvents, ...fromItems].sort((a, b) => a.start.getTime() - b.start.getTime());
}

/** "2026-11-19T09:30" as seen in `tz` (the value format of <input type="datetime-local">). */
export function localInput(d: Date, tz: string): string {
  const p = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: tz,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    })
      .formatToParts(d)
      .map((x) => [x.type, x.value]),
  );
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
}

/** Inverse of localInput: the instant whose wall-clock time in `tz` is `local`. */
export function fromLocal(local: string, tz: string): Date {
  const want = Date.parse(`${local}:00Z`);
  let t = want;
  for (let i = 0; i < 2; i++) t += want - Date.parse(`${localInput(new Date(t), tz)}:00Z`); // 2nd pass: DST edges
  return new Date(t);
}

const pad = (n: number) => String(n).padStart(2, "0");

/** Times after dropping an item on `day` (and `hour` in the day view). "move" keeps the duration and,
 *  without an hour, the time of day; "end" (resize) moves only the end. Null if the end would not follow the start. */
export function dropTimes(
  e: { start: Date; end: Date },
  mode: "move" | "end",
  day: string,
  hour: number | null,
  tz: string,
): { start: Date; end: Date } | null {
  if (mode === "move") {
    const t = localInput(e.start, tz).slice(11);
    const start = fromLocal(`${day}T${hour === null ? t : `${pad(hour)}:${t.slice(3)}`}`, tz);
    return { start, end: new Date(start.getTime() + e.end.getTime() - e.start.getTime()) };
  }
  const end =
    hour === null
      ? fromLocal(`${day}T${localInput(e.end, tz).slice(11)}`, tz)
      : new Date(fromLocal(`${day}T${pad(hour)}:00`, tz).getTime() + 3_600_000); // to the end of that hour
  return end > e.start ? { start: e.start, end } : null;
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
