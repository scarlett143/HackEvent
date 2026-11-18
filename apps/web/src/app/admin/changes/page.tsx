"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { adminRedirect, api, type ApiError } from "@/lib/api";
import { FEED_REFRESH_MS, formatWhen } from "@/lib/events";
import { Icon } from "../../symbol";
import { AppShell, useShell } from "../../ui";
import { AdminNav } from "../admin-nav";

// Admin "Changes" (owner decision 2026-11-19): what the crawler found recently (new events,
// moved deadlines, cancellations) and a manual "Refresh now" that makes every source due
// (api/admin_changes.py). Titles and organisers are plain text from the API.

const HERE = "/admin/changes";
type Change = {
  event_id: string;
  title: string;
  organiser: string;
  change: "new" | "deadline_changed" | "cancelled";
  at: string;
  registration_deadline: string | null;
  priority: boolean;
};
type Changes = {
  crawl: {
    sources_enabled: number;
    last_success_at: string | null;
    next_run_at: string | null;
    refresh_available_at: string | null;
  };
  items: Change[];
};
type Refresh = { sources_queued: number; events_linked: number; refresh_available_at: string };

const LABEL = { new: "New", deadline_changed: "Deadline moved", cancelled: "Cancelled" } as const;
const WINDOWS = [24, 72, 168] as const;

export default function ChangesPage() {
  return (
    <AppShell>
      <ChangesView />
    </AppShell>
  );
}

function ChangesView() {
  const router = useRouter();
  const { tz } = useShell();
  const [hours, setHours] = useState<(typeof WINDOWS)[number]>(24);
  const [priorityOnly, setPriorityOnly] = useState(true);
  const [data, setData] = useState<Changes | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const fail = useCallback(
    (r: { status: number; error: ApiError }) => {
      const to = adminRedirect(r, HERE);
      if (to) router.replace(to);
      else setError(r.error.message);
    },
    [router],
  );
  const load = useCallback(
    () => api<Changes>(`/api/v1/admin/changes?hours=${hours}`).then((r) => (r.ok ? setData(r.data) : fail(r))),
    [hours, fail],
  );
  useEffect(() => {
    load();
    const t = setInterval(load, FEED_REFRESH_MS); // same rhythm as the feed
    return () => clearInterval(t);
  }, [load]);

  async function refreshNow() {
    setError(null);
    setNotice(null);
    setBusy(true);
    const r = await api<Refresh>("/api/v1/admin/refresh", "POST");
    setBusy(false);
    if (!r.ok) return r.status === 429 ? setError(r.error.message) : fail(r);
    setNotice(
      `Queued ${r.data.sources_queued} source(s); linked ${r.data.events_linked} event(s) to listed organisations. ` +
        "New results appear here within a few minutes.",
    );
    await load();
  }

  const crawl = data?.crawl;
  const coolingDown = !!crawl?.refresh_available_at && new Date(crawl.refresh_available_at) > new Date();
  const items = (data?.items ?? []).filter((i) => !priorityOnly || i.priority);

  return (
    <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col gap-6 p-4 md:p-6">
      <header className="flex flex-col gap-3 border-b border-line pb-4">
        <h1 className="font-display text-[1.75rem] leading-8 font-bold">Changes</h1>
        <AdminNav current="changes" />
      </header>

      <section aria-labelledby="crawl-heading" className="card flex flex-wrap items-center justify-between gap-4 p-4">
        <div className="flex flex-col gap-1 text-sm">
          <h2 id="crawl-heading" className="font-display text-lg font-semibold">
            Crawler
          </h2>
          {crawl ? (
            <>
              <p>
                {crawl.sources_enabled} source(s) enabled, each checked every hour or faster.
              </p>
              <p className="text-muted">
                Last successful fetch: {crawl.last_success_at ? formatWhen(crawl.last_success_at, tz) : "never"}
                {" · "}Next due: {crawl.next_run_at ? formatWhen(crawl.next_run_at, tz) : "none"}
              </p>
            </>
          ) : (
            <p className="text-muted">Loading…</p>
          )}
        </div>
        <div className="flex flex-col items-end gap-1">
          <button
            type="button"
            className="btn-primary inline-flex min-h-11 items-center gap-2"
            onClick={refreshNow}
            disabled={busy || coolingDown}
          >
            <Icon name="refresh" size={18} className={busy ? "animate-spin" : undefined} />
            {busy ? "Refreshing…" : "Refresh now"}
          </button>
          {coolingDown && crawl?.refresh_available_at && (
            <p className="text-muted text-xs">Available again at {formatWhen(crawl.refresh_available_at, tz)}</p>
          )}
        </div>
      </section>

      <div aria-live="polite" className="min-h-5 text-sm">
        {error && (
          <p role="alert" className="text-urgent">
            {error}
          </p>
        )}
        {notice && <p>{notice}</p>}
      </div>

      <div className="flex flex-wrap items-end gap-4">
        <label className="flex flex-col gap-1 text-sm font-medium">
          Window
          <select className="field" value={hours} onChange={(e) => setHours(+e.target.value as typeof hours)}>
            {WINDOWS.map((h) => (
              <option key={h} value={h}>
                {h === 24 ? "Last 24 hours" : `Last ${h / 24} days`}
              </option>
            ))}
          </select>
        </label>
        <label className="flex min-h-11 items-center gap-2 text-sm font-medium">
          <input
            type="checkbox"
            className="size-5"
            checked={priorityOnly}
            onChange={(e) => setPriorityOnly(e.target.checked)}
          />
          Feed events only (Fortune companies, tier 1 colleges)
        </label>
      </div>

      {!data && <div aria-busy="true" aria-label="Loading changes" className="skeleton h-48" />}
      {data && items.length === 0 && (
        <div className="card p-6">
          <p className="font-display text-xl font-semibold">No changes in this window</p>
          <p className="text-muted text-sm">New hackathons, moved deadlines and cancellations show up here.</p>
        </div>
      )}
      {items.length > 0 && (
        <ul className="flex flex-col gap-2">
          {items.map((i) => (
            <li key={`${i.event_id}:${i.change}`} className="card flex flex-wrap items-center gap-3 p-3">
              <span className="chip px-2 text-xs font-medium">{LABEL[i.change]}</span>
              <div className="flex min-w-0 flex-1 flex-col">
                <Link href={`/events/${i.event_id}`} className="link truncate font-medium">
                  {i.title}
                </Link>
                <span className="text-muted truncate text-sm">
                  {i.organiser}
                  {!i.priority && " · not in the feed"}
                  {i.registration_deadline && ` · deadline ${formatWhen(i.registration_deadline, tz)}`}
                </span>
              </div>
              <time className="text-muted text-sm" dateTime={i.at}>
                {formatWhen(i.at, tz)}
              </time>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
