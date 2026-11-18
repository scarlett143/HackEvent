"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { adminRedirect, api, type ApiError } from "@/lib/api";
import { formatWhen, safeHttpUrl } from "@/lib/events";
import { AppShell, useShell } from "../../ui";
import { AdminNav } from "../admin-nav";

// App Flow F11 / doc 06 A7 (HE-093): needs-review events with their source snippets beside
// the extracted fields; Approve, Edit + approve, Merge with…, Reject. Snippets and reasons are
// plain text from the API and rendered as text.
// ponytail: edit times are entered in the browser's local time (datetime-local); a per-field
// timezone picker if admins ever review from another zone than their own.

const HERE = "/admin/review";
type Source = { url: string; snippet: string; extractor: string; found_at: string; reasons: string[] };
type Duplicate = {
  id: string;
  title: string;
  organiser: string;
  registration_deadline: string | null;
  event_start: string | null;
  similarity: number;
};
type Item = {
  id: string;
  title: string;
  organiser: string;
  registration_url: string;
  registration_deadline: string | null;
  event_start: string | null;
  event_end: string | null;
  mode: string;
  location: string | null;
  confidence: number;
  first_seen_at: string;
  sources: Source[];
  duplicates: Duplicate[];
};
type Queue = { items: Item[]; total: number };
type Decision = { action: "approve" | "reject" | "merge"; merge_into?: string; patch?: Record<string, string> };

/** ISO (UTC) -> value for <input type="datetime-local"> in the browser's zone. */
function toLocalInput(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export default function ReviewPage() {
  return (
    <AppShell>
      <Review />
    </AppShell>
  );
}

function Review() {
  const router = useRouter();
  const { tz } = useShell();
  const [queue, setQueue] = useState<Queue | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const fail = useCallback(
    (r: { status: number; error: ApiError }) => {
      const to = adminRedirect(r, HERE);
      if (to) router.replace(to);
      else setError(r.error.message);
    },
    [router],
  );
  const load = useCallback(
    () => api<Queue>("/api/v1/admin/review").then((r) => (r.ok ? setQueue(r.data) : fail(r))),
    [fail],
  );
  useEffect(() => {
    load();
  }, [load]);

  async function decide(item: Item, body: Decision) {
    setError(null);
    setNotice(null);
    const r = await api(`/api/v1/admin/review/${item.id}`, "POST", body);
    if (!r.ok) return r.status === 422 ? setError(r.error.message) : fail(r);
    const verb = { approve: "approved", reject: "rejected", merge: "merged" }[body.action];
    setNotice(`"${item.title}" ${verb}.`);
    await load();
  }

  return (
    <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col gap-6 p-4 md:p-6">
      <header className="flex flex-col gap-3 border-b border-line pb-4">
        <h1 className="font-display text-[1.75rem] leading-8 font-bold">Review queue</h1>
        <AdminNav current="review" />
      </header>
      <div aria-live="polite" className="min-h-5 text-sm">
        {error && (
          <p role="alert" className="text-urgent">
            {error}
          </p>
        )}
        {notice && <p>{notice}</p>}
      </div>
      {!queue && <div aria-busy="true" aria-label="Loading review queue" className="skeleton h-48" />}
      {queue && queue.items.length === 0 && (
        <div className="card p-6">
          <p className="font-display text-xl font-semibold">Nothing to review</p>
          <p className="text-muted text-sm">Events the extractor was unsure about show up here.</p>
        </div>
      )}
      {queue && queue.items.length > 0 && (
        <p className="text-muted text-sm">
          {queue.total} waiting{queue.total > queue.items.length ? `, oldest ${queue.items.length} shown` : ""}
        </p>
      )}
      <ul className="flex flex-col gap-4">
        {queue?.items.map((it) => <ReviewCard key={it.id} it={it} tz={tz} onDecide={(b) => decide(it, b)} />)}
      </ul>
    </main>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <dt className="text-muted text-xs font-semibold uppercase">{label}</dt>
      <dd className="text-sm">{value}</dd>
    </div>
  );
}

function ReviewCard({ it, tz, onDecide }: { it: Item; tz: string; onDecide: (b: Decision) => void }) {
  const [editing, setEditing] = useState(false);
  const [mergeInto, setMergeInto] = useState(it.duplicates[0]?.id ?? "");
  const url = safeHttpUrl(it.registration_url);
  const when = (iso: string | null) => (iso ? formatWhen(iso, tz) : "Not stated");

  function submitEdit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    const patch: Record<string, string> = {};
    const title = String(f.get("title") ?? "").trim();
    if (title && title !== it.title) patch.title = title;
    const link = String(f.get("registration_url") ?? "").trim();
    if (link && link !== it.registration_url) patch.registration_url = link;
    for (const key of ["registration_deadline", "event_start"] as const) {
      const v = String(f.get(key) ?? "");
      if (v && v !== toLocalInput(it[key])) patch[key] = new Date(v).toISOString();
    }
    onDecide({ action: "approve", patch });
  }

  return (
    <li className="card flex flex-col gap-4 p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 className="font-display text-xl font-semibold">{it.title}</h2>
          <p className="text-muted text-sm">
            {it.organiser} · confidence {it.confidence.toFixed(2)} · found {formatWhen(it.first_seen_at, tz)}
          </p>
        </div>
        {url && (
          <a className="link text-sm" href={url} target="_blank" rel="noopener noreferrer">
            Registration page<span className="sr-only"> (opens in a new tab)</span>
          </a>
        )}
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <section aria-label={`Extracted fields: ${it.title}`}>
          <dl className="grid grid-cols-2 gap-3">
            <Field label="Deadline" value={when(it.registration_deadline)} />
            <Field label="Starts" value={when(it.event_start)} />
            <Field label="Ends" value={when(it.event_end)} />
            <Field label="Mode" value={it.location ? `${it.mode} · ${it.location}` : it.mode} />
          </dl>
        </section>
        <section aria-label={`Source evidence: ${it.title}`} className="flex flex-col gap-2">
          {it.sources.map((s) => (
            <figure key={s.url} className="rounded-lg border border-line p-3 text-sm">
              <blockquote className="whitespace-pre-line">{s.snippet || "No snippet"}</blockquote>
              <figcaption className="text-muted mt-2 text-xs">
                {s.extractor} · {formatWhen(s.found_at, tz)} ·{" "}
                {safeHttpUrl(s.url) ? (
                  <a className="link" href={s.url} target="_blank" rel="noopener noreferrer">
                    source
                  </a>
                ) : (
                  "source"
                )}
              </figcaption>
              {s.reasons.length > 0 && (
                <p className="text-warning-ink mt-1 text-xs font-semibold">Why: {s.reasons.join(", ")}</p>
              )}
            </figure>
          ))}
        </section>
      </div>

      {editing && (
        <form onSubmit={submitEdit} className="grid gap-3 rounded-lg border border-line p-3 md:grid-cols-2" aria-label={`Edit ${it.title}`}>
          <label className="flex flex-col gap-1 text-sm font-medium">
            Title
            <input name="title" defaultValue={it.title} maxLength={200} required className="field" />
          </label>
          <label className="flex flex-col gap-1 text-sm font-medium">
            Registration URL
            <input name="registration_url" type="url" defaultValue={it.registration_url} className="field" />
          </label>
          <label className="flex flex-col gap-1 text-sm font-medium">
            Registration deadline (your local time)
            <input name="registration_deadline" type="datetime-local" defaultValue={toLocalInput(it.registration_deadline)} className="field" />
          </label>
          <label className="flex flex-col gap-1 text-sm font-medium">
            Event start (your local time)
            <input name="event_start" type="datetime-local" defaultValue={toLocalInput(it.event_start)} className="field" />
          </label>
          <div className="flex gap-2 md:col-span-2">
            <button className="btn-primary">Save and approve</button>
            <button type="button" className="btn-secondary" onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
        </form>
      )}

      <div className="flex flex-wrap items-end gap-2">
        <button className="btn-primary" onClick={() => onDecide({ action: "approve" })}>
          Approve<span className="sr-only">: {it.title}</span>
        </button>
        {!editing && (
          <button className="btn-secondary" onClick={() => setEditing(true)}>
            Edit + approve<span className="sr-only">: {it.title}</span>
          </button>
        )}
        <label className="flex flex-col gap-1 text-sm font-medium">
          Merge with
          <select className="field" value={mergeInto} onChange={(e) => setMergeInto(e.target.value)}>
            <option value="">Choose an event…</option>
            {it.duplicates.map((d) => (
              <option key={d.id} value={d.id}>
                {d.title} ({d.organiser}, {Math.round(d.similarity * 100)}% similar)
              </option>
            ))}
          </select>
        </label>
        <button
          className="btn-secondary"
          disabled={!mergeInto}
          onClick={() => onDecide({ action: "merge", merge_into: mergeInto })}
        >
          Merge<span className="sr-only">: {it.title}</span>
        </button>
        <button className="btn-secondary text-urgent" onClick={() => onDecide({ action: "reject" })}>
          Reject<span className="sr-only">: {it.title}</span>
        </button>
      </div>
    </li>
  );
}
