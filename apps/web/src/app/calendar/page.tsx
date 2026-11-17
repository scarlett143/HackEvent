"use client";

// Calendar (Stitch calendar / week / day / agenda): the events you track or have registered for,
// as deadlines and event runs. Built client-side from GET /events; no calendar API needed.

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";
import {
  addDays,
  agenda,
  type Entry,
  entriesFor,
  entriesOnDay,
  dayKeyOf,
  dayLabel,
  monthGrid,
  monthLabel,
  step,
  timeLabel,
  type View,
  VIEWS,
  weekDays,
} from "@/lib/calendar-view";
import { type EventItem, formatDeadline, urgency } from "@/lib/events";
import { loadEvents } from "@/lib/load-events";
import { downloadIcs } from "../calendar-menu";
import { AppShell, useNow, useShell } from "../ui";

const VIEW_LABEL: Record<View, string> = { month: "Month", week: "Week", day: "Day", agenda: "Agenda" };
const DOW = ["S", "M", "T", "W", "T", "F", "S"];

export default function CalendarPage() {
  return (
    <Suspense fallback={<p className="text-muted p-8">Loading…</p>}>
      <AppShell>
        <Calendar />
      </AppShell>
    </Suspense>
  );
}

function Calendar() {
  const { tz } = useShell();
  const now = useNow();
  const router = useRouter();
  const sp = useSearchParams();
  const today = dayKeyOf(now, tz);
  const rawView = sp.get("view");
  const view: View = VIEWS.includes(rawView as View) ? (rawView as View) : "month";
  const rawDate = sp.get("date");
  const date = rawDate && /^\d{4}-\d{2}-\d{2}$/.test(rawDate) ? rawDate : today;

  const [events, setEvents] = useState<EventItem[] | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    loadEvents().then((e) => (e ? setEvents(e) : setFailed(true)));
  }, []);

  const go = (next: { view?: View; date?: string }) =>
    router.replace(`/calendar?view=${next.view ?? view}&date=${next.date ?? date}`, { scroll: false });
  const entries = useMemo(() => (events ? entriesFor(events) : []), [events]);
  const tracked = useMemo(() => (events ?? []).filter((e) => e.is_tracked || e.is_registered), [events]);

  return (
    <main className="mx-auto flex w-full max-w-6xl flex-col gap-4 p-4 md:p-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="font-display text-3xl font-bold">Calendar</h1>
          <p className="text-muted text-sm">Times in {tz}. Showing events you track or registered for.</p>
        </div>
        <button
          className="btn-secondary"
          disabled={tracked.length === 0}
          onClick={() => downloadIcs("hackevents.ics", tracked)}
        >
          Export .ics
        </button>
      </header>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-1">
          <button className="btn-secondary min-w-12" aria-label="Previous" onClick={() => go({ date: step(date, view, -1) })}>
            <span aria-hidden>‹</span>
          </button>
          <button className="btn-secondary" onClick={() => go({ date: today })}>
            Today
          </button>
          <button className="btn-secondary min-w-12" aria-label="Next" onClick={() => go({ date: step(date, view, 1) })}>
            <span aria-hidden>›</span>
          </button>
          <h2 className="font-display ml-2 text-xl font-semibold" aria-live="polite">
            {view === "day" ? dayLabel(date) : monthLabel(date)}
          </h2>
        </div>
        <div role="group" aria-label="View" className="flex gap-1">
          {VIEWS.map((v) => (
            <button key={v} className="chip focus-ring min-h-11 px-3 text-sm" aria-pressed={view === v} onClick={() => go({ view: v })}>
              {VIEW_LABEL[v]}
            </button>
          ))}
        </div>
      </div>

      <ul className="text-muted flex flex-wrap gap-4 text-sm" aria-label="Legend">
        <li className="flex items-center gap-2">
          <span aria-hidden className="size-2.5 rounded-full bg-urgent" /> Deadline
        </li>
        <li className="flex items-center gap-2">
          <span aria-hidden className="size-2.5 rounded-full bg-primary" /> Event
        </li>
        <li>✓ Registered</li>
      </ul>

      {failed && (
        <p role="alert" className="text-urgent">
          Could not load your events. Reload to try again.
        </p>
      )}
      {!events && !failed && <div aria-busy="true" aria-label="Loading calendar" className="skeleton h-96" />}
      {events && entries.length === 0 && (
        <div className="card flex flex-col items-start gap-2 p-6">
          <p className="font-medium">Nothing on your calendar yet.</p>
          <p className="text-muted text-sm">Tap the star on an event in the feed, or mark it registered, and it shows up here.</p>
          <Link className="link" href="/">
            Browse the feed
          </Link>
        </div>
      )}
      {events && entries.length > 0 && (
        <>
          {view === "month" && <Month date={date} today={today} entries={entries} tz={tz} now={now} onPick={(d) => go({ date: d })} />}
          {view === "week" && <Week date={date} today={today} entries={entries} tz={tz} now={now} />}
          {view === "day" && <Day date={date} entries={entries} tz={tz} now={now} />}
          {view === "agenda" && <Agenda from={date < today ? today : date} entries={entries} tz={tz} now={now} />}
        </>
      )}
    </main>
  );
}

// ── Pieces ───────────────────────────────────────────────────────────────────

function EntryRow({ e, tz, now }: { e: Entry; tz: string; now: Date }) {
  const deadline = e.kind === "deadline";
  const u = deadline ? urgency(e.end.toISOString(), now, tz) : "none";
  const tone = u === "urgent" ? "text-urgent" : u === "warning" ? "text-warning-ink" : u === "ok" ? "text-ok-ink" : "text-muted";
  return (
    <li className="card flex gap-3 p-3">
      <span aria-hidden className={`mt-1.5 size-2.5 shrink-0 rounded-full ${deadline ? "bg-urgent" : "bg-primary"}`} />
      <div className="flex min-w-0 flex-col gap-0.5">
        <p className="text-muted text-sm tabular-nums">
          {deadline ? "Deadline" : "Event"} · {timeLabel(e.start, tz)}
          {!deadline && ` – ${timeLabel(e.end, tz)}`}
          {e.registered && " · ✓ Registered"}
        </p>
        <Link href={`/events/${encodeURIComponent(e.eventId)}`} className="focus-ring font-display rounded text-lg font-semibold hover:underline">
          {e.title}
        </Link>
        {deadline ? (
          <p className={`text-sm font-medium ${tone}`}>{formatDeadline(e.end.toISOString(), now, tz)}</p>
        ) : (
          e.location && <p className="text-muted text-sm">{e.location}</p>
        )}
      </div>
    </li>
  );
}

function Month({
  date, today, entries, tz, now, onPick,
}: { date: string; today: string; entries: Entry[]; tz: string; now: Date; onPick: (d: string) => void }) {
  const grid = monthGrid(date);
  const picked = entriesOnDay(entries, date, tz);
  return (
    <div className="flex flex-col gap-4">
      <div className="card grid grid-cols-7 overflow-hidden text-center">
        {DOW.map((d, i) => (
          <div key={i} className="text-muted border-b border-line py-2 text-xs font-semibold" aria-hidden>
            {d}
          </div>
        ))}
        {grid.map((day) => {
          const on = entriesOnDay(entries, day, tz);
          const inMonth = day.slice(0, 7) === date.slice(0, 7);
          return (
            <button
              key={day}
              type="button"
              aria-pressed={day === date}
              aria-label={`${dayLabel(day)}, ${on.length} ${on.length === 1 ? "item" : "items"}`}
              onClick={() => onPick(day)}
              className={`focus-ring flex min-h-14 flex-col items-center gap-1 border-b border-line p-1 text-sm sm:min-h-20 ${
                day === date ? "bg-primary-soft" : "hover:bg-surface"
              } ${inMonth ? "" : "text-muted opacity-50"}`}
            >
              <span className={`flex size-7 items-center justify-center rounded-full tabular-nums ${day === today ? "bg-primary font-bold text-on-primary" : ""}`}>
                {Number(day.slice(8))}
              </span>
              <span aria-hidden className="flex gap-0.5">
                {on.slice(0, 3).map((e) => (
                  <span key={e.key} className={`size-1.5 rounded-full ${e.kind === "deadline" ? "bg-urgent" : "bg-primary"}`} />
                ))}
              </span>
            </button>
          );
        })}
      </div>
      <section aria-label={`Items on ${dayLabel(date)}`} className="flex flex-col gap-2">
        <h3 className="font-display text-lg font-semibold">{dayLabel(date)}</h3>
        {picked.length === 0 ? (
          <p className="text-muted text-sm">Nothing on this day.</p>
        ) : (
          <ul className="flex flex-col gap-2">{picked.map((e) => <EntryRow key={e.key} e={e} tz={tz} now={now} />)}</ul>
        )}
      </section>
    </div>
  );
}

function Week({ date, today, entries, tz, now }: { date: string; today: string; entries: Entry[]; tz: string; now: Date }) {
  return (
    <div className="grid gap-3 md:grid-cols-7">
      {weekDays(date).map((day) => {
        const on = entriesOnDay(entries, day, tz);
        return (
          <section key={day} aria-label={dayLabel(day)} className="flex flex-col gap-2">
            <h3 className={`font-display rounded-lg px-2 py-1 text-base font-semibold ${day === today ? "bg-primary text-on-primary" : "surface"}`}>
              {dayLabel(day)}
            </h3>
            {on.length === 0 ? (
              <p className="text-muted px-2 text-sm">—</p>
            ) : (
              <ul className="flex flex-col gap-2">{on.map((e) => <EntryRow key={e.key} e={e} tz={tz} now={now} />)}</ul>
            )}
          </section>
        );
      })}
    </div>
  );
}

const HOURS = Array.from({ length: 24 }, (_, h) => h);

function Day({ date, entries, tz, now }: { date: string; entries: Entry[]; tz: string; now: Date }) {
  const on = entriesOnDay(entries, date, tz);
  const hourOf = (d: Date) => Number(timeLabel(d, tz).slice(0, 2));
  const isToday = dayKeyOf(now, tz) === date;
  const nowHour = hourOf(now);
  const hard = on.filter((e) => e.kind === "deadline").length;
  // Business hours always; the small hours only when something lands in them.
  const rows = HOURS.filter((h) => (h >= 8 && h <= 23) || on.some((e) => dayKeyOf(e.start, tz) === date && hourOf(e.start) === h));
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm font-medium">
        {on.length} {on.length === 1 ? "item" : "items"} scheduled · {hard} hard {hard === 1 ? "deadline" : "deadlines"}
      </p>
      <ol className="flex flex-col">
        {rows.map((h) => {
          const here = on.filter((e) => (dayKeyOf(e.start, tz) === date ? hourOf(e.start) === h : h === 0));
          return (
            <li key={h} className="flex gap-3 border-t border-line py-2">
              <span className="text-muted w-14 shrink-0 text-sm tabular-nums">{String(h).padStart(2, "0")}:00</span>
              <div className="flex min-w-0 flex-1 flex-col gap-2">
                {isToday && h === nowHour && (
                  <p className="text-urgent text-xs font-semibold" aria-label="Current time">
                    Now {timeLabel(now, tz)}
                  </p>
                )}
                {here.length > 0 && <ul className="flex flex-col gap-2">{here.map((e) => <EntryRow key={e.key} e={e} tz={tz} now={now} />)}</ul>}
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function Agenda({ from, entries, tz, now }: { from: string; entries: Entry[]; tz: string; now: Date }) {
  const groups = agenda(entries, from, tz);
  if (groups.length === 0) return <p className="text-muted">Nothing upcoming from {dayLabel(from)}.</p>;
  return (
    <div className="flex flex-col gap-5">
      {groups.map(([day, list]) => (
        <section key={day} aria-label={dayLabel(day)} className="flex flex-col gap-2">
          <h3 className="font-display flex items-center gap-3 text-lg font-semibold">
            {dayLabel(day)}
            {day === addDays(from, 0) && from === dayKeyOf(now, tz) && <span className="chip">Today</span>}
            <span aria-hidden className="h-px flex-1 bg-line" />
          </h3>
          <ul className="flex flex-col gap-2">{list.map((e) => <EntryRow key={e.key} e={e} tz={tz} now={now} />)}</ul>
        </section>
      ))}
    </div>
  );
}
