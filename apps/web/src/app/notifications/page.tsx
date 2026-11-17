"use client";

// Notifications centre (Stitch "Alerts"): deadline, new-event and registration alerts derived from
// the events API (lib/alerts.ts), with per-item and bulk mark-read. Read state is per browser.
// Squad invites / system trust items have no data source yet and are not shown.

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import { type Alert, type AlertKind, buildAlerts, newActionAlerts, parseRead, READ_KEY, serializeRead, unreadCount } from "@/lib/alerts";
import { safeHttpUrl } from "@/lib/events";
import { loadEvents } from "@/lib/load-events";
import { Icon } from "../symbol";
import { AppShell, Avatar, EventMeta, useShell } from "../ui";

type Tab = "all" | AlertKind;
const TABS: [Tab, string][] = [
  ["all", "All"],
  ["deadline", "Deadlines"],
  ["new", "New"],
  ["registered", "Registered"],
];
const LEVEL = {
  urgent: { label: "Urgent", tone: "border-urgent/30 bg-urgent/10 text-urgent", icon: "alarm" },
  warning: { label: "Warning", tone: "border-warning/30 bg-warning/10 text-warning-ink", icon: "hourglass_top" },
  info: { label: "", tone: "", icon: "" },
} as const;
const KIND_ICON = { deadline: "hourglass_top", new: "bolt", registered: "event_available" } as const;
const KIND_LABEL: Record<AlertKind, string> = { deadline: "Deadline", new: "New", registered: "Registered" };

export default function NotificationsPage() {
  return (
    <AppShell>
      <Notifications />
    </AppShell>
  );
}

function Notifications() {
  const { tz, refreshUnread, toast } = useShell();
  const known = useRef<Set<string> | null>(null); // ids from the previous refresh; null until the first load
  const [alerts, setAlerts] = useState<Alert[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [read, setRead] = useState<Set<string>>(new Set());
  const [tab, setTab] = useState<Tab>("all");

  useEffect(() => {
    // Mount-time loads: state is set after an await / from storage, once.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setRead(parseRead(localStorage.getItem(READ_KEY)));
    let first = true;
    const load = () =>
      loadEvents().then((ev) => {
        if (!ev) return first && setFailed(true);
        const next = buildAlerts(ev, new Date(), tz);
        const fresh = newActionAlerts(known.current, next);
        known.current = new Set(next.map((a) => a.id));
        first = false;
        setAlerts(next);
        if (fresh.length)
          toast({ message: fresh.length === 1 ? `${fresh[0].title}: ${fresh[0].detail}` : `${fresh.length} deadlines need action` });
      });
    load();
    const id = setInterval(() => document.visibilityState === "visible" && load(), 60_000);
    return () => clearInterval(id);
  }, [tz, toast]);

  const persist = useCallback((next: Set<string>) => {
    setRead(next);
    try {
      localStorage.setItem(READ_KEY, serializeRead(next));
    } catch {
      // Private mode: read state just won't survive a reload.
    }
  }, []);

  const markRead = (ids: string[]) => persist(new Set([...read, ...ids]));
  async function markAllRead() {
    markRead((alerts ?? []).map((a) => a.id));
    const r = await api("/api/v1/me/seen", "POST"); // also clears the server-side "new" badge
    if (r.ok) refreshUnread();
  }

  const shown = useMemo(() => (alerts ?? []).filter((a) => tab === "all" || a.kind === tab), [alerts, tab]);
  const unread = alerts ? unreadCount(alerts, read) : 0;
  const count = (t: Tab) => (alerts ?? []).filter((a) => t === "all" || a.kind === t).length;
  const urgentGroup = shown.filter((a) => a.level !== "info");
  const updates = shown.filter((a) => a.level === "info");

  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4 md:p-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="font-display text-[1.75rem] leading-8 font-bold">Notifications</h1>
          <p className="text-muted text-sm" aria-live="polite">
            {alerts ? `${unread} unread of ${alerts.length}` : "Loading…"}
          </p>
        </div>
        <button className="btn-secondary inline-flex items-center gap-2" disabled={!alerts || unread === 0} onClick={markAllRead}>
          <Icon name="done_all" />
          Mark all read
        </button>
      </header>

      <div role="group" aria-label="Filter notifications" className="flex flex-wrap gap-2">
        {TABS.map(([t, label]) => (
          <button key={t} className="chip chip-tab focus-ring min-h-11 px-3 text-sm" aria-pressed={tab === t} onClick={() => setTab(t)}>
            {label} ({count(t)})
          </button>
        ))}
      </div>

      {failed && (
        <p role="alert" className="text-urgent">
          Could not load notifications. Reload to try again.
        </p>
      )}
      {!alerts && !failed && <div aria-busy="true" aria-label="Loading notifications" className="skeleton h-48" />}
      {alerts && shown.length === 0 && (
        <div className="card flex flex-col items-start gap-2 p-6">
          <p className="font-display text-xl font-semibold">All caught up!</p>
          <p className="text-muted text-sm">
            Deadlines for events you track, new hackathons and registrations show up here.
          </p>
          <Link className="link" href="/">
            Browse the feed
          </Link>
        </div>
      )}
      <Group title="Needs action" list={urgentGroup} read={read} onRead={(id) => markRead([id])} />
      <Group title="Updates" list={updates} read={read} onRead={(id) => markRead([id])} />
    </main>
  );
}

function Group({
  title, list, read, onRead,
}: { title: string; list: Alert[]; read: Set<string>; onRead: (id: string) => void }) {
  if (list.length === 0) return null;
  return (
    <section aria-label={title} className="flex flex-col gap-3">
      <h2 className="font-display flex items-center gap-3 text-xl font-semibold">
        {title}
        <span className="chip">{list.length}</span>
        <span aria-hidden className="h-px flex-1 bg-line" />
      </h2>
      <ul className="flex flex-col gap-3">
        {list.map((a) => (
          <AlertCard key={a.id} a={a} isRead={read.has(a.id)} onRead={() => onRead(a.id)} />
        ))}
      </ul>
    </section>
  );
}

function AlertCard({ a, isRead, onRead }: { a: Alert; isRead: boolean; onRead: () => void }) {
  const lvl = LEVEL[a.level];
  const url = safeHttpUrl(a.event.registration_url);
  return (
    <li className={`card flex gap-3 p-4 ${isRead ? "opacity-60" : ""}`}>
      <Avatar name={a.title} />
      <div className="flex min-w-0 flex-1 flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2">
          {lvl.label ? (
            <span className={`chip ${lvl.tone}`}>
              <Icon name={lvl.icon} size={16} className="mr-1" />
              {lvl.label}
            </span>
          ) : (
            <span className="chip">
              <Icon name={KIND_ICON[a.kind]} size={16} className="mr-1" />
              {KIND_LABEL[a.kind]}
            </span>
          )}
          {!isRead && (
            <span className="flex items-center gap-1 text-xs font-semibold">
              <span aria-hidden className="size-2 rounded-full bg-new" />
              Unread
            </span>
          )}
        </div>
        <h3 className="font-display text-lg font-semibold">
          <Link href={`/events/${encodeURIComponent(a.event.id)}`} className="focus-ring rounded hover:underline" onClick={onRead}>
            {a.title}
          </Link>
        </h3>
        <p className="text-sm font-medium">{a.detail}</p>
        <EventMeta ev={a.event} />
        <div className="flex flex-wrap items-center gap-2">
          {url && a.kind === "deadline" && (
            <a className="btn-primary inline-flex items-center" href={url} target="_blank" rel="noopener noreferrer" onClick={onRead}>
              Register <Icon name="arrow_outward" className="ml-1" />
              <span className="sr-only"> (opens the registration page in a new tab)</span>
            </a>
          )}
          <Link className="btn-secondary inline-flex items-center" href={`/events/${encodeURIComponent(a.event.id)}`} onClick={onRead}>
            View event
          </Link>
          {!isRead && (
            <button className="link focus-ring min-h-11 text-sm" onClick={onRead}>
              Mark read<span className="sr-only">: {a.title}</span>
            </button>
          )}
        </div>
      </div>
    </li>
  );
}
