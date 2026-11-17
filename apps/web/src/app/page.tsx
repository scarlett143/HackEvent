"use client";

// Feed (design brief §4.1, App Flow F2/F3): filters in the URL, deadline-sorted sections,
// cursor paging, preview pane on wide screens. HE-051 + HE-053.

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import {
  type EventItem,
  eventsQuery,
  FILTER_OPTIONS,
  type Filters,
  filtersToSearch,
  formatWhen,
  type Page,
  parseFilters,
  sectionize,
} from "@/lib/events";
import { Icon } from "./symbol";
import { AppShell, Chips, Countdown, EventActions, EventMeta, OrgAvatar, TrackStar, useNow, useShell } from "./ui";

const SEEN_AFTER_MS = 3_000;
const LATER_SHOWN = 5;
// Stitch quick-filter chips: label, filter key, value.
const QUICK = [
  ["Tier 1", "tier", "1", "workspace_premium"],
  ["Online", "mode", "online", "wifi"],
  ["India", "region", "india", "location_on"],
  ["Global", "region", "global", "public"],
] as const;

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
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <form
          role="search"
          className="flex min-w-60 flex-1 gap-2"
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
          {(["kind", "tier", "mode", "status", "within"] as const).map((k) => (
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
        <h2 className="font-display flex items-center gap-3 text-xl font-semibold">
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
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_22rem]">
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
        <div className="sticky top-4">
          <EventPreview ev={preview} now={now} tz={tz} />
        </div>
      </aside>
    </div>
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
    <article
      className="card flex flex-col gap-3 p-4"
      onClick={() => onSelect(ev.id)}
      onFocus={() => onSelect(ev.id)}
    >
      <div className="flex items-start gap-3">
        <OrgAvatar name={ev.organiser.name} />
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <div className="flex flex-wrap items-center gap-1.5">
            {ev.is_new && (
              <span className="rounded bg-new px-1.5 py-0.5 text-xs font-bold tracking-wide text-white">NEW</span>
            )}
            {ev.organiser.tier && (
              <span className="chip gap-1">
                <Icon name="workspace_premium" size={16} />
                Tier {ev.organiser.tier}
              </span>
            )}
          </div>
          <h3 className="font-display text-xl font-semibold">
            <Link href={`/events/${encodeURIComponent(ev.id)}`} className="focus-ring rounded hover:underline">
              {ev.title}
            </Link>
          </h3>
          <EventMeta ev={ev} />
        </div>
        <TrackStar ev={ev} onPatch={onPatch} ghost />
      </div>
      <div className="surface rounded-lg px-3 py-2">
        <Countdown deadline={ev.registration_deadline} now={now} tz={tz} />
      </div>
      <Chips ev={ev} />
      <EventActions ev={ev} onPatch={onPatch} />
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
      <OrgAvatar name={ev.organiser.name} />
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

function EventPreview({ ev, now, tz }: { ev: EventItem; now: Date; tz: string }) {
  return (
    <div className="flex flex-col gap-3 rounded-xl border border-line p-4">
      <h2 className="text-lg font-semibold">{ev.title}</h2>
      <EventMeta ev={ev} />
      <Countdown deadline={ev.registration_deadline} now={now} tz={tz} />
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
        <dt className="text-muted">Event</dt>
        <dd>{ev.event_start ? formatWhen(ev.event_start, tz) : "Dates not listed"}</dd>
        {ev.location && (
          <>
            <dt className="text-muted">Where</dt>
            <dd>{ev.location}</dd>
          </>
        )}
      </dl>
      <Chips ev={ev} />
      <Link className="link" href={`/events/${encodeURIComponent(ev.id)}`}>
        Open details and source
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
