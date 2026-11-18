"use client";

// Calendar (Stitch calendar / week / day / agenda): deadlines of the events you track or registered for
// (GET /events) plus your calendar items (GET /calendar-items, HE-060): personal events and your own copy
// of each registered event's run. Items are draggable (native HTML5 drag and drop): drop on a day or hour
// to move, drag the handle to resize; every change is PATCHed. Click an hour (day view) or "New event" to create.

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { createContext, type DragEvent, Suspense, useContext, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import {
  addDays,
  agenda,
  type CalendarItem,
  dropTimes,
  type Entry,
  entriesFor,
  entriesOnDay,
  dayKeyOf,
  dayLabel,
  fromLocal,
  localInput,
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
import { downloadIcs, downloadXls } from "../calendar-menu";
import { Icon } from "../symbol";
import { AppShell, useNow, useShell } from "../ui";

const VIEW_LABEL: Record<View, string> = { month: "Month", week: "Week", day: "Day", agenda: "Agenda" };
const DOW = ["S", "M", "T", "W", "T", "F", "S"];
const MS_DAY = 86_400_000;
const HOUR_MS = 3_600_000;

/** Draft of the create / edit form; `start` and `end` are datetime-local values in the user's timezone. */
type Draft = { id: string | null; title: string; start: string; end: string };
type DropProps = { onDragOver: (e: DragEvent) => void; onDrop: (e: DragEvent) => void };
/** What the views can do to items, without threading props through every view. */
type Actions = {
  target: (day: string, hour: number | null) => DropProps;
  edit: (e: Entry) => void;
  create: (day: string, hour: number) => void;
};
const ActionsCtx = createContext<Actions | null>(null);
const useActions = () => useContext(ActionsCtx)!;

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
  const { tz, toast } = useShell();
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
  const [items, setItems] = useState<CalendarItem[] | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  useEffect(() => {
    loadEvents().then((e) => (e ? setEvents(e) : setFailed(true)));
    // ponytail: one fixed window (1 year back, 2 ahead) instead of refetching per view; page it if it grows.
    const q = new URLSearchParams({
      from: new Date(Date.now() - 365 * MS_DAY).toISOString(),
      to: new Date(Date.now() + 730 * MS_DAY).toISOString(),
    });
    api<{ items: CalendarItem[] }>(`/api/v1/calendar-items?${q}`).then((r) => (r.ok ? setItems(r.data.items) : setFailed(true)));
  }, []);

  const itemUrl = (id: string) => `/api/v1/calendar-items/${encodeURIComponent(id)}`;
  /** Optimistic drag/resize: show the new times at once, roll back if the API says no. */
  const move = async (id: string, start: Date, end: Date) => {
    const body = { starts_at: start.toISOString(), ends_at: end.toISOString() };
    const before = items;
    setItems((l) => l && l.map((i) => (i.id === id ? { ...i, ...body } : i)));
    const r = await api<CalendarItem>(itemUrl(id), "PATCH", body);
    if (r.ok) setItems((l) => l && l.map((i) => (i.id === id ? r.data : i)));
    else {
      setItems(before);
      toast({ message: `Could not move it: ${r.error.message}` });
    }
  };
  const save = async (d: Draft): Promise<string | null> => {
    const body = { title: d.title.trim(), starts_at: fromLocal(d.start, tz).toISOString(), ends_at: fromLocal(d.end, tz).toISOString() };
    const r = d.id
      ? await api<CalendarItem>(itemUrl(d.id), "PATCH", body)
      : await api<CalendarItem>("/api/v1/calendar-items", "POST", body);
    if (!r.ok) return r.error.message;
    setItems((l) => (l ? (d.id ? l.map((i) => (i.id === d.id ? r.data : i)) : [...l, r.data]) : l));
    setDraft(null);
    return null;
  };
  const remove = async (id: string): Promise<string | null> => {
    const r = await api<null>(itemUrl(id), "DELETE");
    if (!r.ok) return r.error.message;
    setItems((l) => l && l.filter((i) => i.id !== id));
    setDraft(null);
    return null;
  };

  const go = (next: { view?: View; date?: string }) =>
    router.replace(`/calendar?view=${next.view ?? view}&date=${next.date ?? date}`, { scroll: false });
  const entries = useMemo(() => (events && items ? entriesFor(events, items) : []), [events, items]);
  const actions: Actions = {
    target: (day, hour) => ({
      onDragOver: (e) => e.preventDefault(),
      onDrop: (e) => {
        e.preventDefault();
        const [mode, id] = e.dataTransfer.getData("text/plain").split(":");
        const en = entries.find((x) => x.itemId === id);
        if (!en || (mode !== "move" && mode !== "end")) return;
        const t = dropTimes(en, mode, day, hour, tz);
        if (t && (t.start.getTime() !== en.start.getTime() || t.end.getTime() !== en.end.getTime())) void move(id, t.start, t.end);
      },
    }),
    edit: (e) => {
      if (e.itemId) setDraft({ id: e.itemId, title: e.title, start: localInput(e.start, tz), end: localInput(e.end, tz) });
    },
    create: (day, hour) => {
      const start = fromLocal(`${day}T${String(hour).padStart(2, "0")}:00`, tz);
      setDraft({ id: null, title: "", start: localInput(start, tz), end: localInput(new Date(start.getTime() + HOUR_MS), tz) });
    },
  };
  const ready = events && items;
  const tracked = useMemo(() => (events ?? []).filter((e) => e.is_tracked || e.is_registered), [events]);
  // Stitch "Radar layers": which kinds of entries the calendar shows.
  const [layers, setLayers] = useState({ registered: true, deadlines: true, watchlisted: true });
  const shown = useMemo(
    () =>
      entries.filter(
        (e) =>
          e.eventId === null || // personal items are always shown
          ((e.kind !== "deadline" || layers.deadlines) && (e.registered ? layers.registered : layers.watchlisted)),
      ),
    [entries, layers],
  );

  return (
    <main className="mx-auto flex w-full max-w-6xl flex-col gap-4 p-4 md:p-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="font-display text-[1.75rem] leading-8 font-bold">Calendar</h1>
          <p className="text-muted text-sm">Times in {tz}. Showing events you track or registered for, and your own events.</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button className="btn-primary inline-flex items-center gap-2" disabled={!items} onClick={() => actions.create(date, 9)}>
            <Icon name="add_circle" />
            New event
          </button>
          <button
            className="btn-secondary inline-flex items-center gap-2"
            disabled={tracked.length === 0}
            onClick={() => downloadXls("hackevents.xls", tracked, tz)}
          >
            <Icon name="file_download" />
            Export .xls
          </button>
          <button
            className="btn-secondary inline-flex items-center gap-2"
            disabled={tracked.length === 0}
            onClick={() => downloadIcs("hackevents.ics", tracked)}
          >
            <Icon name="file_download" />
            Export .ics
          </button>
        </div>
      </header>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-1">
          <button className="btn-secondary min-w-12" aria-label="Previous" onClick={() => go({ date: step(date, view, -1) })}>
            <Icon name="chevron_left" size={24} />
          </button>
          <button className="btn-secondary" onClick={() => go({ date: today })}>
            Today
          </button>
          <button className="btn-secondary min-w-12" aria-label="Next" onClick={() => go({ date: step(date, view, 1) })}>
            <Icon name="chevron_right" size={24} />
          </button>
          <h2 className="font-display ml-2 text-xl font-semibold" aria-live="polite">
            {view === "day" ? dayLabel(date) : monthLabel(date)}
          </h2>
        </div>
        <div role="group" aria-label="View" className="flex gap-1">
          {VIEWS.map((v) => (
            <button key={v} className="chip chip-tab focus-ring min-h-11 px-3 text-sm" aria-pressed={view === v} onClick={() => go({ view: v })}>
              {VIEW_LABEL[v]}
            </button>
          ))}
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-[18rem_minmax(0,1fr)]">
        <CalendarSide layers={layers} setLayers={setLayers} entries={entries} now={now} tz={tz} />
        <div className="flex min-w-0 flex-col gap-4">
      <ul className="flex flex-wrap gap-x-6 gap-y-2 text-base font-medium" aria-label="Legend">
        <li className="flex items-center gap-2">
          <span aria-hidden className="size-4 rounded-sm bg-urgent" /> Deadline
        </li>
        <li className="flex items-center gap-2">
          <span aria-hidden className="size-4 rounded-full bg-primary" /> Event
        </li>
        <li className="flex items-center gap-2">
          <Icon name="check_circle" fill size={22} className="text-ok-ink" /> Registered
        </li>
      </ul>

      {draft && <ItemForm key={draft.id ?? "new"} draft={draft} onSave={save} onDelete={remove} onCancel={() => setDraft(null)} />}
      {failed && (
        <p role="alert" className="text-urgent">
          Could not load your events. Reload to try again.
        </p>
      )}
      {!ready && !failed && <div aria-busy="true" aria-label="Loading calendar" className="skeleton h-96" />}
      {ready && entries.length === 0 && (
        <div className="card flex flex-col items-start gap-2 p-6">
          <p className="font-medium">Nothing on your calendar yet.</p>
          <p className="text-muted text-sm">Tap the star on an event in the feed, mark it registered, or add your own with New event.</p>
          <Link className="link" href="/">
            Browse the feed
          </Link>
        </div>
      )}
      {ready && entries.length > 0 && (
        <ActionsCtx.Provider value={actions}>
          {view === "month" && <Month date={date} today={today} entries={shown} tz={tz} now={now} onPick={(d) => go({ date: d })} />}
          {view === "week" && <Week date={date} today={today} entries={shown} tz={tz} now={now} />}
          {view === "day" && <Day date={date} entries={shown} tz={tz} now={now} />}
          {view === "agenda" && <Agenda from={date < today ? today : date} entries={shown} tz={tz} now={now} />}
        </ActionsCtx.Provider>
      )}
        </div>
      </div>
    </main>
  );
}

// ── Pieces ───────────────────────────────────────────────────────────────────

function ItemForm({
  draft, onSave, onDelete, onCancel,
}: {
  draft: Draft;
  onSave: (d: Draft) => Promise<string | null>;
  onDelete: (id: string) => Promise<string | null>;
  onCancel: () => void;
}) {
  const [d, setD] = useState(draft);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const run = async (p: Promise<string | null>) => {
    setBusy(true);
    setError(await p);
    setBusy(false);
  };
  return (
    <form
      aria-label={draft.id ? "Edit event" : "New event"}
      className="card flex flex-col gap-3 p-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (d.end < d.start) setError("The end must not be before the start.");
        else void run(onSave(d));
      }}
    >
      <h2 className="font-display text-lg font-semibold">{draft.id ? "Edit event" : "New event"}</h2>
      <label className="flex flex-col gap-1 text-sm font-medium">
        Title
        <input className="field" required maxLength={200} autoFocus value={d.title} onChange={(e) => setD({ ...d, title: e.target.value })} />
      </label>
      <div className="flex flex-wrap gap-3">
        <label className="flex flex-col gap-1 text-sm font-medium">
          Starts
          <input className="field" type="datetime-local" required value={d.start} onChange={(e) => setD({ ...d, start: e.target.value })} />
        </label>
        <label className="flex flex-col gap-1 text-sm font-medium">
          Ends
          <input className="field" type="datetime-local" required value={d.end} onChange={(e) => setD({ ...d, end: e.target.value })} />
        </label>
      </div>
      {error && (
        <p role="alert" className="text-urgent text-sm">
          {error}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <button className="btn-primary" disabled={busy || !d.title.trim()}>
          Save
        </button>
        <button type="button" className="btn-secondary" onClick={onCancel}>
          Cancel
        </button>
        {draft.id && (
          <button type="button" className="btn-secondary text-urgent" disabled={busy} onClick={() => void run(onDelete(draft.id!))}>
            Delete
          </button>
        )}
      </div>
    </form>
  );
}

const dragAs = (mode: "move" | "end", id: string) => (ev: DragEvent) => {
  ev.stopPropagation(); // the resize handle sits inside the draggable row
  ev.dataTransfer.setData("text/plain", `${mode}:${id}`);
  ev.dataTransfer.effectAllowed = "move";
};

function EntryRow({ e, tz, now }: { e: Entry; tz: string; now: Date }) {
  const act = useActions();
  const deadline = e.kind === "deadline";
  const u = deadline ? urgency(e.end.toISOString(), now, tz) : "none";
  const tone = u === "urgent" ? "text-urgent" : u === "warning" ? "text-warning-ink" : u === "ok" ? "text-ok-ink" : "text-muted";
  return (
    <li
      className={`card flex gap-3 p-3 ${e.itemId ? "cursor-grab" : ""}`}
      draggable={!!e.itemId}
      onDragStart={e.itemId ? dragAs("move", e.itemId) : undefined}
      data-item={e.itemId ?? undefined}
    >
      <span aria-hidden className={`mt-1 size-3.5 shrink-0 ${deadline ? "rounded-sm bg-urgent" : "rounded-full bg-primary"}`} />
      <div className="flex min-w-0 flex-col gap-0.5">
        <p className="text-muted text-sm tabular-nums">
          {deadline ? "Deadline" : e.eventId ? "Event" : "Personal"} · {timeLabel(e.start, tz)}
          {!deadline && ` – ${timeLabel(e.end, tz)}`}
          {e.registered && (
            <>
              {" · "}
              <Icon name="check_circle" fill size={16} className="text-ok-ink align-[-3px]" /> Registered
            </>
          )}
        </p>
        {e.eventId ? (
          <Link href={`/events/${encodeURIComponent(e.eventId)}`} className="focus-ring font-display rounded text-lg font-semibold hover:underline">
            {e.title}
          </Link>
        ) : (
          <p className="font-display text-lg font-semibold">{e.title}</p>
        )}
        {deadline ? (
          <p className={`text-sm font-medium ${tone}`}>{formatDeadline(e.end.toISOString(), now, tz)}</p>
        ) : (
          e.location && <p className="text-muted text-sm">{e.location}</p>
        )}
      </div>
      {e.itemId && (
        <div className="ml-auto flex shrink-0 flex-col items-end gap-1">
          <button className="link focus-ring min-h-11 text-sm" onClick={() => act.edit(e)} aria-label={`Edit ${e.title}`}>
            Edit
          </button>
          <span
            draggable
            onDragStart={dragAs("end", e.itemId)}
            data-resize={e.itemId}
            title="Drag to a day or hour to change when it ends"
            className="text-muted cursor-ns-resize text-xs"
          >
            <span aria-hidden>⇕</span> Resize
          </span>
        </div>
      )}
    </li>
  );
}

function Month({
  date, today, entries, tz, now, onPick,
}: { date: string; today: string; entries: Entry[]; tz: string; now: Date; onPick: (d: string) => void }) {
  const act = useActions();
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
              {...act.target(day, null)}
              data-day={day}
              className={`focus-ring flex min-h-16 flex-col items-center gap-1 border-b border-line p-1 text-sm sm:min-h-24 lg:min-h-28 lg:items-stretch ${
                day === date ? "bg-primary-soft" : "hover:bg-surface"
              } ${inMonth ? "" : "text-muted opacity-50"}`}
            >
              <span className={`flex size-8 items-center justify-center rounded-full tabular-nums ${day === today ? "bg-ink font-bold text-bg" : ""}`}>
                {Number(day.slice(8))}
              </span>
              <span aria-hidden className="flex items-center gap-1 lg:hidden">
                {on.slice(0, 3).map((e) => (
                  <span key={e.key} className={`size-3 ${e.kind === "deadline" ? "rounded-sm bg-urgent" : "rounded-full bg-primary"}`} />
                ))}
                {on.some((e) => e.registered) && <Icon name="check_circle" fill size={16} className="text-ok-ink" />}
              </span>
              <span aria-hidden className="hidden w-full flex-col gap-0.5 lg:flex">
                {on.slice(0, 2).map((e) => (
                  <span
                    key={e.key}
                    className={`truncate rounded px-1 text-left text-xs font-medium ${
                      e.kind === "deadline" ? "bg-urgent/10 text-urgent" : "bg-primary-soft text-ink"
                    }`}
                  >
                    {e.registered && <Icon name="check" size={16} className="mr-0.5 align-[-3px]" />}
                    {e.title}
                  </span>
                ))}
                {on.length > 2 && <span className="text-muted text-left text-xs">+{on.length - 2} more</span>}
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
  const act = useActions();
  return (
    <div className="grid gap-3 md:grid-cols-7">
      {weekDays(date).map((day) => {
        const on = entriesOnDay(entries, day, tz);
        return (
          <section key={day} aria-label={dayLabel(day)} className="flex flex-col gap-2" {...act.target(day, null)} data-day={day}>
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
  const act = useActions();
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
            <li key={h} className="flex gap-3 border-t border-line py-2" {...act.target(date, h)} data-hour={h}>
              <button
                className="text-muted focus-ring w-14 shrink-0 self-start rounded text-left text-sm tabular-nums hover:underline"
                aria-label={`New event at ${String(h).padStart(2, "0")}:00`}
                onClick={() => act.create(date, h)}
              >
                {String(h).padStart(2, "0")}:00
              </button>
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

type Layers = { registered: boolean; deadlines: boolean; watchlisted: boolean };
const CUTOFF_WINDOW_MS = 48 * 3_600_000;

/** "26h 14m" until `end`. */
const hoursLeft = (end: Date, now: Date) => {
  const mins = Math.max(0, Math.floor((end.getTime() - now.getTime()) / 60_000));
  return `${Math.floor(mins / 60)}h ${String(mins % 60).padStart(2, "0")}m`;
};

/** Stitch desktop calendar side panel: radar layers (filters) and the 48-hour cutoffs list. */
function CalendarSide({
  layers, setLayers, entries, now, tz,
}: { layers: Layers; setLayers: (l: Layers) => void; entries: Entry[]; now: Date; tz: string }) {
  const cutoffs = entries.filter(
    (e) => e.kind === "deadline" && e.end > now && e.end.getTime() - now.getTime() <= CUTOFF_WINDOW_MS,
  );
  const ROWS: [keyof Layers, string, string][] = [
    ["registered", "Registered events", "bg-ok"],
    ["deadlines", "Hard cutoff deadlines", "bg-urgent"],
    ["watchlisted", "Watchlisted hackathons", "bg-tertiary"],
  ];
  return (
    <aside className="flex flex-col gap-4">
      <section aria-labelledby="layers-h" className="card flex flex-col gap-2 p-4">
        <div className="flex items-center justify-between">
          <h2 id="layers-h" className="text-muted text-xs font-semibold tracking-[0.12em] uppercase">
            Radar layers
          </h2>
          <button className="link focus-ring min-h-11 text-sm" onClick={() => setLayers({ registered: true, deadlines: true, watchlisted: true })}>
            Reset all
          </button>
        </div>
        {ROWS.map(([k, label, dot]) => (
          <label key={k} className="flex min-h-11 items-center gap-3 text-sm">
            <span aria-hidden className={`size-2.5 rounded-full ${dot}`} />
            <span className="flex-1">{label}</span>
            <input
              type="checkbox"
              className="size-5 accent-[var(--color-link)]"
              checked={layers[k]}
              onChange={(e) => setLayers({ ...layers, [k]: e.target.checked })}
            />
          </label>
        ))}
      </section>
      <section aria-labelledby="cutoffs-h" className="card flex flex-col gap-3 p-4">
        <div className="flex items-center justify-between">
          <h2 id="cutoffs-h" className="font-display flex items-center gap-2 text-lg font-semibold">
            <Icon name="timer" className="text-urgent" /> 48-hr cutoffs
          </h2>
          {cutoffs.length > 0 && (
            <span className="chip border-urgent/30 bg-urgent/10 text-urgent">
              {cutoffs.length} {cutoffs.length === 1 ? "alert" : "alerts"}
            </span>
          )}
        </div>
        {cutoffs.length === 0 ? (
          <p className="text-muted text-sm">No registration closes in the next 48 hours.</p>
        ) : (
          <ul className="flex flex-col gap-2">
            {cutoffs.map((e) => (
              <li key={e.key} className="surface flex flex-col gap-1 rounded-lg p-3">
                <div className="flex items-center justify-between gap-2">
                  <p className="text-urgent text-xs font-semibold tracking-wide uppercase">Registration closes</p>
                  <span className="chip border-urgent/30 bg-urgent/10 text-urgent tabular-nums">{hoursLeft(e.end, now)}</span>
                </div>
                <Link href={`/events/${encodeURIComponent(e.eventId)}`} className="focus-ring font-display rounded text-lg font-semibold hover:underline">
                  {e.title}
                </Link>
                <p className="text-muted text-sm tabular-nums">{timeLabel(e.end, tz)} · {dayLabel(dayKeyOf(e.end, tz))}</p>
              </li>
            ))}
          </ul>
        )}
      </section>
    </aside>
  );
}
