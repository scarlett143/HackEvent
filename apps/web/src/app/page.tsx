"use client";

// Feed (design brief §4.1, App Flow F2/F3): filters in the URL, deadline-sorted sections,
// cursor paging, preview pane on wide screens. HE-051 + HE-053.

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import {
  daysBetween,
  type EventDetail,
  type EventItem,
  eventsQuery,
  FILTER_OPTIONS,
  type Filters,
  filtersToSearch,
  formatWhen,
  type Page,
  parseFilters,
  sectionize,
  tMinus,
  urgency,
} from "@/lib/events";
import { AddToCalendar } from "./calendar-menu";
import { Icon } from "./symbol";
import { AppShell, Avatar, Chips, Countdown, EventActions, EventMeta, SourceBlock, TrackStar, useNow, useShell } from "./ui";

const SEEN_AFTER_MS = 3_000;
const LATER_SHOWN = 5;
// Stitch quick-filter chips: label, filter key, value.
const QUICK = [
  ["Online", "mode", "online", "wifi"],
  ["Closing soon", "status", "closing", "alarm"],
  ["Registered", "registered", true, "check_circle"],
] as const;
// Organisation-based filters are not offered; ignore them if an old link still carries them.
const HIDDEN_FILTERS = ["kind", "tier", "region"] as const;

const LABELS: { [K in keyof typeof FILTER_OPTIONS]: [string, Record<string, string>] } = {
  kind: ["Type", { company: "Company", institution: "Institution" }],
  tier: ["Tier", { "1": "Tier 1", "2": "Tier 2", "3": "Tier 3" }],
  region: ["Region", { india: "India", global: "Global" }],
  mode: ["Mode", { online: "Online", offline: "In person", hybrid: "Hybrid" }],
  status: ["Status", { open: "Open", closing: "Closing soon", closed: "Closed" }],
  within: ["Deadline", { "3": "Next 3 days", "7": "Next 7 days", "30": "Next 30 days" }],
};

export default function Home() {
  return (
    // URL hooks (useSearchParams/usePathname) must sit under Suspense with cacheComponents.
    <Suspense fallback={<p className="text-muted p-8">Loading…</p>}>
      <AppShell>
        <Feed />
      </AppShell>
    </Suspense>
  );
}

function Feed() {
  const router = useRouter();
  const sp = useSearchParams();
  const filters = parseFilters(new URLSearchParams(sp.toString()));
  for (const k of HIDDEN_FILTERS) delete filters[k];
  const key = filtersToSearch(filters);

  const apply = (next: Filters) => {
    const qs = filtersToSearch(next);
    router.replace(qs ? `/?${qs}` : "/", { scroll: false });
  };

  return (
    <main className="flex flex-col gap-4 p-4 md:p-6">
      <h1 className="sr-only">Hackathon feed</h1>
      {/* Remount both on every filter change (fresh form state, fresh list + cursor).
          Sibling keys must differ: a shared key made React keep the old FilterBar
          and add a new one on every search. */}
      <FilterBar key={`filters:${key}`} filters={filters} apply={apply} />
      <Results key={`results:${key}`} query={key} />
    </main>
  );
}

// ── Filters (HE-053) ─────────────────────────────────────────────────────────

function FilterBar({ filters, apply }: { filters: Filters; apply: (f: Filters) => void }) {
  const active = Object.keys(filters).filter((k) => k !== "q" && k !== "region").length;
  const set = (patch: Partial<Filters>) => {
    const next: Record<string, unknown> = { ...filters, ...patch };
    for (const k of Object.keys(next)) if (!next[k]) delete next[k];
    apply(next as Filters);
  };

  return (
    <div className="card flex flex-col gap-3 p-3">
      <div className="flex flex-wrap items-center gap-2 md:hidden">
        <form
          role="search"
          className="flex min-w-60 flex-1 gap-2 md:hidden"
          onSubmit={(e) => {
            e.preventDefault();
            set({ q: String(new FormData(e.currentTarget).get("q") ?? "").trim() || undefined });
          }}
        >
          <label className="sr-only" htmlFor="feed-q">
            Search events
          </label>
          <div className="relative min-w-0 flex-1">
            <span className="text-muted pointer-events-none absolute top-1/2 left-3 -translate-y-1/2">
              <Icon name="search" />
            </span>
            <input
              id="feed-q"
              name="q"
              type="search"
              placeholder="Search hackathons"
              defaultValue={filters.q ?? ""}
              className="field w-full pl-10"
            />
          </div>
          <button className="btn-secondary inline-flex items-center gap-2" aria-label="Search">
            <Icon name="search" />
            <span className="hidden sm:inline">Search</span>
          </button>
        </form>
      </div>
      <div role="group" aria-label="Quick filters" className="flex flex-wrap gap-2">
        <button
          type="button"
          className="chip focus-ring min-h-11 px-3 text-sm"
          aria-pressed={Object.keys(filters).length === 0}
          onClick={() => apply({})}
        >
          <Icon name="check" size={16} className="mr-1" />
          All events
        </button>
        {QUICK.map(([label, key, value, icon]) => (
          <button
            key={label}
            type="button"
            className="chip focus-ring min-h-11 px-3 text-sm"
            aria-pressed={filters[key] === value}
            onClick={() => set({ [key]: filters[key] === value ? undefined : value })}
          >
            <Icon name={icon} size={16} className="mr-1" />
            {label}
          </button>
        ))}
      </div>
      <details className="rounded-xl border border-line" open={active > 0}>
        <summary className="focus-ring flex min-h-11 cursor-pointer items-center px-3 text-sm font-medium">
          <Icon name="tune" size={18} className="mr-2" />
          Filters{active > 0 && ` (${active})`}
        </summary>
        <div className="flex flex-wrap items-end gap-3 border-t border-line p-3">
          {(["mode", "status", "within"] as const).map((k) => (
            <label key={k} className="flex flex-col gap-1 text-sm font-medium">
              {LABELS[k][0]}
              <select
                className="field"
                value={filters[k] ?? ""}
                onChange={(e) => set({ [k]: e.target.value || undefined })}
              >
                <option value="">Any</option>
                {FILTER_OPTIONS[k].map((v) => (
                  <option key={v} value={v}>
                    {LABELS[k][1][v]}
                  </option>
                ))}
              </select>
            </label>
          ))}
          <label className="flex min-h-11 items-center gap-2 text-sm font-medium">
            <input
              type="checkbox"
              className="size-5"
              checked={!!filters.registered}
              onChange={(e) => set({ registered: e.target.checked || undefined })}
            />
            Registered only
          </label>
          <label className="flex min-h-11 items-center gap-2 text-sm font-medium">
            <input
              type="checkbox"
              className="size-5"
              checked={!!filters.new_only}
              onChange={(e) => set({ new_only: e.target.checked || undefined })}
            />
            New only
          </label>
          {Object.keys(filters).length > 0 && (
            <button type="button" className="link min-h-11" onClick={() => apply({})}>
              Clear all
            </button>
          )}
        </div>
      </details>
    </div>
  );
}

// ── Results ──────────────────────────────────────────────────────────────────

type State =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; items: EventItem[]; cursor: string | null; more: "idle" | "loading" | "error" };

function Results({ query }: { query: string }) {
  const { tz, refreshUnread } = useShell();
  const now = useNow();
  const [state, setState] = useState<State>({ kind: "loading" });
  const [selected, setSelected] = useState<string | null>(null);
  const [showAllLater, setShowAllLater] = useState(false);
  const seen = useRef(false);

  const fetchPage = useCallback(
    (cursor: string | null) =>
      api<Page<EventItem>>(
        `/api/v1/events?${eventsQuery(parseFilters(new URLSearchParams(query)), new Date(), cursor)}`,
      ),
    [query],
  );

  useEffect(() => {
    fetchPage(null).then((r) =>
      setState(
        r.ok
          ? { kind: "ready", items: r.data.items, cursor: r.data.next_cursor, more: "idle" }
          : { kind: "error", message: r.error.message },
      ),
    );
  }, [fetchPage]);

  // App Flow F2 step 3: once the feed has been visible for 3 s, mark it seen.
  const ready = state.kind === "ready";
  useEffect(() => {
    if (!ready || seen.current) return;
    const t = setTimeout(() => {
      if (document.visibilityState !== "visible") return;
      seen.current = true;
      api("/api/v1/me/seen", "POST").then((r) => r.ok && refreshUnread());
    }, SEEN_AFTER_MS);
    return () => clearTimeout(t);
  }, [ready, refreshUnread]);

  const patch = useCallback((id: string, p: Partial<EventItem>) => {
    setState((s) =>
      s.kind === "ready" ? { ...s, items: s.items.map((e) => (e.id === id ? { ...e, ...p } : e)) } : s,
    );
  }, []);

  /** Stitch "Mark all read": same endpoint as the 3 s auto-seen, but explicit. */
  async function markAllRead() {
    const r = await api("/api/v1/me/seen", "POST");
    if (!r.ok) return;
    seen.current = true;
    setState((s) => (s.kind === "ready" ? { ...s, items: s.items.map((e) => ({ ...e, is_new: false })) } : s));
    refreshUnread();
  }

  async function loadMore() {
    if (state.kind !== "ready" || !state.cursor) return;
    setState({ ...state, more: "loading" });
    const r = await fetchPage(state.cursor);
    setState((s) =>
      s.kind !== "ready"
        ? s
        : r.ok
          ? { ...s, items: [...s.items, ...r.data.items], cursor: r.data.next_cursor, more: "idle" }
          : { ...s, more: "error" },
    );
  }

  if (state.kind === "loading") return <FeedSkeleton />;
  if (state.kind === "error")
    return (
      <p role="alert" className="text-urgent">
        Could not load events. {state.message}
      </p>
    );
  if (!state.items.length)
    return (
      <div className="surface flex flex-col items-start gap-2 rounded-xl p-6">
        <p className="font-medium">No events match these filters.</p>
        <Link className="link" href="/">
          Clear filters
        </Link>
      </div>
    );

  const { fresh, closing, later } = sectionize(state.items, now);
  const preview = state.items.find((e) => e.id === selected) ?? state.items[0];
  const section = (title: string, list: EventItem[], aside?: React.ReactNode, compact = false) =>
    list.length > 0 && (
      <section aria-label={title} className="flex flex-col gap-3">
        <h2 className="font-display flex items-center gap-3 text-xl font-semibold tracking-wide uppercase">
          {title}
          <span aria-hidden className="h-px flex-1 bg-line" />
          {aside}
        </h2>
        {list.map((ev) =>
          compact ? (
            <CompactRow key={ev.id} ev={ev} now={now} tz={tz} onPatch={patch} />
          ) : (
            <EventCard key={ev.id} ev={ev} now={now} tz={tz} onPatch={patch} onSelect={setSelected} />
          ),
        )}
      </section>
    );
  const laterShown = showAllLater ? later : later.slice(0, LATER_SHOWN);

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_28rem]">
      <div className="flex min-w-0 flex-col gap-6">
        {section(
          `New since last visit (${fresh.length})`,
          fresh,
          <button type="button" className="link focus-ring min-h-11 text-sm font-medium" onClick={markAllRead}>
            Mark all read
          </button>,
        )}
        {section("Closing this week", closing)}
        {section(
          "Later",
          laterShown,
          later.length > LATER_SHOWN && (
            <button
              type="button"
              className="link focus-ring min-h-11 text-sm font-medium"
              aria-expanded={showAllLater}
              onClick={() => setShowAllLater((v) => !v)}
            >
              {showAllLater ? "Show fewer" : `View ${later.length - LATER_SHOWN} more`}
            </button>
          ),
          true,
        )}
        {state.cursor && (
          <div className="flex flex-col items-center gap-2">
            <button className="btn-secondary" disabled={state.more === "loading"} onClick={loadMore}>
              {state.more === "loading" ? "Loading…" : "Load more"}
            </button>
            {state.more === "error" && (
              <p role="alert" className="text-urgent text-sm">
                Could not load more events. Try again.
              </p>
            )}
          </div>
        )}
      </div>
      <aside aria-label="Event preview" className="hidden xl:block">
        <div className="sticky top-20">
          <EventPreview ev={preview} now={now} tz={tz} onPatch={patch} />
        </div>
      </aside>
    </div>
  );
}

const MODE_LABEL = { online: "Online", offline: "In person", hybrid: "Hybrid", unknown: "Mode not listed" } as const;

const PILL = {
  urgent: "border-urgent/30 bg-urgent/10 text-urgent",
  warning: "border-warning/30 bg-warning/10 text-warning-ink",
  ok: "border-line bg-surface text-ink",
  closed: "border-line bg-surface text-muted",
  none: "border-line bg-surface text-muted",
} as const;

/** Stitch countdown pill: "2d 04h left", tinted by urgency (the text always says it). */
function CountdownPill({ deadline, now, tz }: { deadline: string | null; now: Date; tz: string }) {
  const u = urgency(deadline, now, tz);
  const left = tMinus(deadline, now);
  const text = u === "closed" ? "Closed" : left ? `${left.replace("T-", "")} left` : "No deadline";
  return (
    <span className={`inline-flex min-h-9 items-center gap-1.5 rounded-lg border px-2.5 text-sm font-semibold tabular-nums ${PILL[u]}`}>
      <Icon name={u === "urgent" ? "timer" : "schedule"} size={18} />
      {text}
    </span>
  );
}

function EventCard({
  ev,
  now,
  tz,
  onPatch,
  onSelect,
}: {
  ev: EventItem;
  now: Date;
  tz: string;
  onPatch: (id: string, p: Partial<EventItem>) => void;
  onSelect: (id: string) => void;
}) {
  return (
    <article className="card flex flex-col overflow-hidden" onClick={() => onSelect(ev.id)} onFocus={() => onSelect(ev.id)}>
      <div className="flex flex-col gap-3 p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="flex min-w-0 items-start gap-3">
            <span className="md:hidden">
              <Avatar name={ev.title} />
            </span>
            <ul className="flex flex-wrap gap-1.5" aria-label="Tags">
              {ev.is_new && <li className="rounded bg-new px-1.5 py-0.5 text-xs font-bold tracking-wide text-white">NEW</li>}
              <li className="chip">{MODE_LABEL[ev.mode]}</li>
              {ev.location && <li className="chip">{ev.location}</li>}
            </ul>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <CountdownPill deadline={ev.registration_deadline} now={now} tz={tz} />
            <TrackStar ev={ev} onPatch={onPatch} ghost />
          </div>
        </div>
        <h3 className="font-display text-xl font-semibold">
          <Link href={`/events/${encodeURIComponent(ev.id)}`} className="focus-ring rounded hover:underline">
            {ev.title}
          </Link>
        </h3>
        <Chips ev={ev} />
      </div>
      <div className="surface flex flex-wrap items-center justify-between gap-3 border-t border-line px-4 py-3">
        <p className={`flex items-center gap-1.5 text-sm font-medium ${urgency(ev.registration_deadline, now, tz) === "urgent" ? "text-urgent" : ""}`}>
          <Icon name="event_busy" size={18} />
          {ev.registration_deadline ? `Closes ${formatWhen(ev.registration_deadline, tz)}` : "No deadline listed"}
        </p>
        <EventActions ev={ev} onPatch={onPatch} />
      </div>
    </article>
  );
}

/** Stitch "Later this month" row: title, when, and the reminder star. */
function CompactRow({
  ev,
  now,
  tz,
  onPatch,
}: {
  ev: EventItem;
  now: Date;
  tz: string;
  onPatch: (id: string, p: Partial<EventItem>) => void;
}) {
  return (
    <article className="card flex items-center gap-3 p-3">
      <Avatar name={ev.title} />
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <h3 className="font-display text-lg font-semibold">
          <Link href={`/events/${encodeURIComponent(ev.id)}`} className="focus-ring rounded hover:underline">
            {ev.title}
          </Link>
        </h3>
        <Countdown deadline={ev.registration_deadline} now={now} tz={tz} />
      </div>
      <TrackStar ev={ev} onPatch={onPatch} />
    </article>
  );
}

/** Stitch desktop preview pane: deadline and sprint cards, the main actions, prize and source audit.
 * Prize and sources come from GET /events/:id for the selected event. */
function EventPreview({
  ev,
  now,
  tz,
  onPatch,
}: {
  ev: EventItem;
  now: Date;
  tz: string;
  onPatch: (id: string, p: Partial<EventItem>) => void;
}) {
  const [detail, setDetail] = useState<EventDetail | null>(null);
  useEffect(() => {
    let alive = true;
    api<EventDetail>(`/api/v1/events/${encodeURIComponent(ev.id)}`).then((r) => alive && setDetail(r.ok ? r.data : null));
    return () => {
      alive = false;
    };
  }, [ev.id]);
  const d = detail?.id === ev.id ? detail : null;
  const u = urgency(ev.registration_deadline, now, tz);
  const left = tMinus(ev.registration_deadline, now);
  const sprint =
    ev.event_start && ev.event_end ? daysBetween(new Date(ev.event_start), new Date(ev.event_end), tz) + 1 : null;
  return (
    <div className="card flex flex-col gap-4 p-4">
      <div className="flex flex-col gap-1">
        {ev.is_new && <span className="w-fit rounded bg-new px-1.5 py-0.5 text-xs font-bold text-white">NEW</span>}
        <h2 className="font-display text-2xl font-bold">{ev.title}</h2>
        <EventMeta ev={ev} />
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div className="surface flex flex-col gap-1 rounded-lg p-3">
          <p className="text-muted flex items-center justify-between text-xs font-semibold tracking-wide uppercase">
            Registration closes <Icon name="alarm" size={18} className={u === "urgent" ? "text-urgent" : ""} />
          </p>
          <p className={`font-display text-2xl font-bold tabular-nums ${u === "urgent" ? "text-urgent" : ""}`}>{left ?? (u === "closed" ? "Closed" : "—")}</p>
          <p className="text-sm tabular-nums">{ev.registration_deadline ? formatWhen(ev.registration_deadline, tz) : "Not listed"}</p>
        </div>
        <div className="surface flex flex-col gap-1 rounded-lg p-3">
          <p className="text-muted flex items-center justify-between text-xs font-semibold tracking-wide uppercase">
            Sprint window <Icon name="bolt" size={18} />
          </p>
          <p className="font-display text-2xl font-bold">{sprint ? `${sprint}-day run` : "—"}</p>
          <p className="text-sm tabular-nums">{ev.event_start ? formatWhen(ev.event_start, tz) : "Dates not listed"}</p>
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <EventActions ev={ev} onPatch={onPatch} />
        <AddToCalendar ev={ev} />
      </div>
      {d?.prize && (
        <section aria-label="Prizes" className="surface flex flex-col gap-1 rounded-lg p-3">
          <h3 className="text-muted text-xs font-semibold tracking-wide uppercase">Prize distribution</h3>
          <p className="text-ok-ink font-display text-lg font-semibold whitespace-pre-line">{d.prize}</p>
        </section>
      )}
      {d && d.sources.length > 0 && (
        <section aria-label="Source" className="flex flex-col gap-2">
          <h3 className="text-muted text-xs font-semibold tracking-wide uppercase">Crawler ingestion audit</h3>
          <SourceBlock source={d.sources[0]} tz={tz} />
        </section>
      )}
      <Link className="link text-sm" href={`/events/${encodeURIComponent(ev.id)}`}>
        Open full details
      </Link>
    </div>
  );
}

function FeedSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading events" className="flex flex-col gap-3">
      {[0, 1, 2].map((i) => (
        <div key={i} className="skeleton h-40" />
      ))}
    </div>
  );
}
