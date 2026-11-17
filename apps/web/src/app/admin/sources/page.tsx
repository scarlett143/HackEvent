"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { adminRedirect, api, type ApiError, type Me } from "@/lib/api";
import { filterHealth } from "@/lib/health";
import { AppShell } from "../../ui";
import { AdminNav } from "../admin-nav";
import { TelemetryPanel } from "./telemetry-panel";

// App Flow F10 + doc 06 A7: organisations (tier, CSV import), sources (create/edit,
// Run now) for Admins (HE-091) and source health, red first (HE-092). Fetched text
// (last_error, reason) is rendered as React text, never as HTML.
// ponytail: plain tables; TanStack (design brief §4.6) once column sorting is needed.

const HERE = "/admin/sources";
const KINDS = ["official", "aggregator", "rss", "reddit", "substack", "social"] as const;
const ADAPTERS = ["rss", "api", "selector", "llm"] as const;
const NETWORKS = ["linkedin", "instagram", "x"] as const;
const PARSERS = ["devpost", "unstop"] as const;
const POLL_MS = 3000;
const POLL_LIMIT_MS = 120_000;

type Source = {
  id: string;
  kind: (typeof KINDS)[number];
  network: (typeof NETWORKS)[number] | null;
  url: string;
  adapter: (typeof ADAPTERS)[number];
  selector_config: Record<string, unknown> | null;
  tier_override: number | null;
  cadence_hours: number;
  is_enabled: boolean;
  next_run_at: string;
  last_success_at: string | null;
  last_event_at: string | null;
  error_streak: number;
  empty_streak: number;
  last_error: string | null;
  updated_at: string;
};
type Health = {
  id: string;
  url: string;
  kind: string;
  adapter: string;
  is_enabled: boolean;
  status: "red" | "amber" | "green";
  reason: string;
  last_success_at: string | null;
  last_error: string | null;
  error_streak: number;
  empty_streak: number;
};
type List<T> = { items: T[]; next_cursor: null };

// Design brief §3/§6: colour is decoration only; the text label carries the status.
const STATUS = {
  red: { label: "Error", dot: "bg-urgent", edge: "border-l-urgent", pill: "border-urgent/30 bg-urgent/10 text-urgent" },
  amber: { label: "Degraded", dot: "bg-warning", edge: "border-l-warning", pill: "border-warning/30 bg-warning/10 text-warning-ink" },
  green: { label: "Healthy", dot: "bg-ok", edge: "border-l-ok", pill: "border-ok/30 bg-ok/10 text-ok-ink" },
} as const;
type Failed = { status: number; error: ApiError };

export default function SourcesPage() {
  const router = useRouter();
  const [me, setMe] = useState<Me | null>(null);
  const [sources, setSources] = useState<Source[] | null>(null);
  const [health, setHealth] = useState<Health[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [editing, setEditing] = useState<Source | "new" | null>(null);
  const [runs, setRuns] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [hFilter, setHFilter] = useState<"all" | Health["status"]>("all");
  const [hQuery, setHQuery] = useState("");

  const fail = useCallback(
    (r: Failed) => {
      const to = adminRedirect(r, HERE);
      if (to) router.replace(to);
      else setError(r.error.message);
    },
    [router],
  );

  const loadHealth = useCallback(async () => {
    const h = await api<List<Health>>("/api/v1/admin/sources/health");
    if (!h.ok) return fail(h);
    setHealth(h.data.items);
  }, [fail]);

  const loadAll = useCallback(async () => {
    const [m, s] = await Promise.all([
      api<Me>("/api/v1/me"),
      api<List<Source>>("/api/v1/admin/sources"),
    ]);
    if (!m.ok) return fail(m);
    if (!s.ok) return fail(s);
    setMe(m.data);
    setSources(s.data.items);
    await loadHealth();
  }, [fail, loadHealth]);

  useEffect(() => {
    // Mount-time fetch; every setState in loadAll runs after an await, not synchronously.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    loadAll();
  }, [loadAll]);

  function say(message: string) {
    setError(null);
    setNotice(message);
  }

  async function saveSource(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    const adapter = String(f.get("adapter"));
    const kind = String(f.get("kind"));
    let selector_config: Record<string, unknown> | null = null;
    const raw = String(f.get("selector_config") ?? "").trim();
    if (raw) {
      try {
        selector_config = JSON.parse(raw);
      } catch {
        return setError("Selector config must be valid JSON.");
      }
    }
    if (adapter === "api") selector_config = { ...(selector_config ?? {}), parser: f.get("parser") };
    const cadence = String(f.get("cadence_hours") ?? "").trim();
    const tier = String(f.get("tier_override") ?? "");
    const body: Record<string, unknown> = {
      url: String(f.get("url") ?? "").trim(),
      kind,
      adapter,
      network: kind === "social" ? f.get("network") : null,
      tier_override: tier ? Number(tier) : null,
      selector_config,
      is_enabled: f.get("is_enabled") === "on",
    };
    // Blank cadence on create = derived from tier; on edit = keep (re-derived on tier change).
    if (cadence) body.cadence_hours = Number(cadence);
    setBusy(true);
    const isNew = editing === "new";
    const r = isNew
      ? await api<Source>("/api/v1/admin/sources", "POST", body)
      : await api<Source>(`/api/v1/admin/sources/${(editing as Source).id}`, "PATCH", body);
    setBusy(false);
    // FastAPI's own 422 (field types/ranges) has no `error` body; ours carry a code.
    if (!r.ok && r.status === 422 && !["invalid_source", "url_blocked"].includes(r.error.code))
      return setError("Check the fields: a value is missing or out of range.");
    if (!r.ok) return fail(r);
    setSources((list) =>
      isNew ? [...(list ?? []), r.data] : (list ?? []).map((x) => (x.id === r.data.id ? r.data : x)),
    );
    setEditing(null);
    loadHealth();
    say(isNew ? `Source added: ${r.data.url}` : `Source saved: ${r.data.url}`);
  }

  async function deleteSource(s: Source) {
    if (!window.confirm(`Delete the source ${s.url}?`)) return;
    const r = await api<void>(`/api/v1/admin/sources/${s.id}`, "DELETE");
    if (!r.ok) return fail(r);
    setSources((list) => list?.filter((x) => x.id !== s.id) ?? null);
    setHealth((list) => list?.filter((x) => x.id !== s.id) ?? null);
    say(`Source deleted: ${s.url}`);
  }

  async function runNow(s: Source) {
    const setRun = (text: string) => setRuns((m) => ({ ...m, [s.id]: text }));
    const start = await api<Source>(`/api/v1/admin/sources/${s.id}`);
    if (!start.ok) return fail(start);
    const before = start.data;
    const r = await api<{ job_id: string }>(`/api/v1/admin/sources/${s.id}/run`, "POST");
    if (!r.ok) return setRun(r.error.message);
    setRun("Queued. Waiting for the result…");
    for (let i = 0; i < POLL_LIMIT_MS / POLL_MS; i++) {
      await new Promise((ok) => setTimeout(ok, POLL_MS));
      const now = await api<Source>(`/api/v1/admin/sources/${s.id}`);
      if (!now.ok) return setRun(now.error.message);
      const cur = now.data;
      // record_scrape sets last_success_at on success, error_streak + 1 on failure.
      const done =
        cur.last_success_at !== before.last_success_at || cur.error_streak > before.error_streak;
      if (!done) continue;
      setSources((list) => list?.map((x) => (x.id === cur.id ? cur : x)) ?? null);
      loadHealth();
      return setRun(
        cur.error_streak > before.error_streak
          ? `Failed: ${cur.last_error ?? "unknown error"}`
          : `Fetched OK at ${when(cur.last_success_at)}.`,
      );
    }
    setRun("No result after 2 minutes. The scrape queue may be busy; check again later.");
  }

  const when = (iso: string | null) =>
    iso
      ? new Intl.DateTimeFormat("en-IN", {
          dateStyle: "medium",
          timeStyle: "short",
          timeZone: me?.timezone,
        }).format(new Date(iso))
      : "Never";

  const shownHealth = filterHealth(health ?? [], hFilter, hQuery);
  const count = (k: Health["status"]) => (health ?? []).filter((h) => h.status === k).length;

  return (
    <AppShell>
    <main className="mx-auto flex w-full max-w-7xl flex-1 flex-col gap-8 p-4 md:p-6">
      <header className="flex flex-col gap-3 border-b border-line pb-4">
        <h1 className="font-display text-[1.75rem] leading-8 font-bold">Sources</h1>
        <AdminNav current="sources" />
      </header>

      <div aria-live="polite" className="min-h-5 text-sm">
        {error && (
          <p role="alert" className="text-urgent">
            {error}
          </p>
        )}
        {notice && <p>{notice}</p>}
      </div>

      <TelemetryPanel
        scrapers={health ? { healthy: count("green"), degraded: count("amber"), error: count("red") } : null}
      />

      <section aria-labelledby="health-heading" className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 id="health-heading" className="text-lg font-semibold">
            Source health
          </h2>
          <button className="btn-secondary" onClick={() => loadHealth()}>
            Refresh
          </button>
        </div>
        <p className="text-muted text-sm">
          Red after 3 failed runs or 5 runs with no events in a row. Admins get an email when a
          source turns red.
        </p>
        <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Filter by status">
          {(
            [
              ["all", `All sources (${health?.length ?? 0})`],
              ["green", `Healthy (${count("green")})`],
              ["amber", `Degraded (${count("amber")})`],
              ["red", `Errors only (${count("red")})`],
            ] as const
          ).map(([k, label]) => (
            <button
              key={k}
              type="button"
              className="chip focus-ring min-h-11 px-3 text-sm"
              aria-pressed={hFilter === k}
              onClick={() => setHFilter(k)}
            >
              {label}
            </button>
          ))}
          <input
            type="search"
            aria-label="Search sources"
            placeholder="Search sources"
            value={hQuery}
            onChange={(e) => setHQuery(e.target.value)}
            className="field ml-auto w-full sm:w-64"
          />
        </div>
        {!health ? (
          <p className="text-muted">Loading…</p>
        ) : health.length === 0 ? (
          <p className="text-muted">No sources yet.</p>
        ) : shownHealth.length === 0 ? (
          <p className="text-muted">No sources match.</p>
        ) : (
          <ul className="grid gap-3 lg:grid-cols-2">
            {shownHealth.map((h) => {
              const src = sources?.find((x) => x.id === h.id);
              return (
                <li key={h.id} className={`card flex flex-col gap-3 border-l-4 p-4 ${STATUS[h.status].edge}`}>
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <h3 className="font-display text-lg font-semibold break-all">{h.url}</h3>
                      <p className="text-muted text-sm">
                        {h.kind} / {h.adapter}
                        {h.is_enabled ? "" : " · disabled"}
                      </p>
                    </div>
                    <span className={`chip shrink-0 gap-2 ${STATUS[h.status].pill}`}>
                      <span aria-hidden="true" className={`size-2 rounded-full ${STATUS[h.status].dot}`} />
                      {STATUS[h.status].label}
                    </span>
                  </div>
                  <dl className="surface grid grid-cols-3 gap-2 rounded-lg p-3 text-center text-sm">
                    <div>
                      <dt className="text-muted text-xs">Last success</dt>
                      <dd className="font-medium">{when(h.last_success_at)}</dd>
                    </div>
                    <div>
                      <dt className="text-muted text-xs">Failed runs</dt>
                      <dd className="font-medium tabular-nums">{h.error_streak}</dd>
                    </div>
                    <div>
                      <dt className="text-muted text-xs">Empty runs</dt>
                      <dd className="font-medium tabular-nums">{h.empty_streak}</dd>
                    </div>
                  </dl>
                  <p className="text-sm break-words">
                    <span className="font-medium">{h.reason}</span>
                    {h.last_error && <span className="text-urgent block">{h.last_error}</span>}
                  </p>
                  <div className="flex flex-wrap items-center gap-3">
                    {src && (
                      <button className="btn-secondary" onClick={() => runNow(src)}>
                        {h.status === "red" ? "Retry source" : "Run scraper"}
                      </button>
                    )}
                    {runs[h.id] && (
                      <span role="status" className="text-muted text-sm">
                        {runs[h.id]}
                      </span>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section aria-labelledby="sources-heading" className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 id="sources-heading" className="text-lg font-semibold">
            Sources
          </h2>
          {editing === null && (
            <button className="btn-primary" onClick={() => setEditing("new")}>
              Add source
            </button>
          )}
        </div>

        {editing !== null && (
          <SourceForm
            key={editing === "new" ? "new" : editing.id}
            source={editing === "new" ? null : editing}
            busy={busy}
            onSubmit={saveSource}
            onCancel={() => setEditing(null)}
          />
        )}

        {!sources ? (
          <p className="text-muted">Loading…</p>
        ) : sources.length === 0 ? (
          <p className="text-muted">No sources yet.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[48rem] text-left text-sm">
              <thead className="surface">
                <tr>
                  <th scope="col" className="p-2">URL</th>
                  <th scope="col" className="p-2">Kind / adapter</th>
                  <th scope="col" className="p-2">Every</th>
                  <th scope="col" className="p-2">Enabled</th>
                  <th scope="col" className="p-2">Last success</th>
                  <th scope="col" className="p-2">Failed runs</th>
                  <th scope="col" className="p-2">Last error</th>
                  <th scope="col" className="p-2">Actions</th>
                </tr>
              </thead>
              <tbody>
                {sources.map((s) => (
                  <tr key={s.id} className="border-t border-line align-top">
                    <th scope="row" className="max-w-64 p-2 font-medium break-all">
                      {s.url}
                    </th>
                    <td className="p-2">
                      {s.kind}
                      {s.network ? ` (${s.network})` : ""} / {s.adapter}
                    </td>
                    <td className="p-2">{s.cadence_hours} h</td>
                    <td className="p-2">{s.is_enabled ? "Yes" : "No"}</td>
                    <td className="p-2">{when(s.last_success_at)}</td>
                    <td className="p-2">{s.error_streak}</td>
                    <td className="max-w-64 p-2 break-words">{s.last_error ?? "None"}</td>
                    <td className="p-2">
                      <div className="flex flex-wrap gap-2">
                        <button
                          className="btn-secondary"
                          aria-label={`Run ${s.url} now`}
                          onClick={() => runNow(s)}
                        >
                          Run now
                        </button>
                        <button
                          className="btn-secondary"
                          aria-label={`Edit ${s.url}`}
                          onClick={() => setEditing(s)}
                        >
                          Edit
                        </button>
                        <button
                          className="btn-secondary"
                          aria-label={`Delete ${s.url}`}
                          onClick={() => deleteSource(s)}
                        >
                          Delete
                        </button>
                      </div>
                      <p aria-live="polite" className="mt-1 max-w-64 break-words">
                        {runs[s.id]}
                      </p>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

    </main>
    </AppShell>
  );
}

function SourceForm({
  source,
  busy,
  onSubmit,
  onCancel,
}: {
  source: Source | null;
  busy: boolean;
  onSubmit: (e: React.FormEvent<HTMLFormElement>) => void;
  onCancel: () => void;
}) {
  const [kind, setKind] = useState<string>(source?.kind ?? "official");
  const [adapter, setAdapter] = useState<string>(source?.adapter ?? "llm");
  const config = { ...(source?.selector_config ?? {}) };
  const parser = typeof config.parser === "string" ? config.parser : "devpost";
  if (adapter === "api") delete config.parser;
  const configText = Object.keys(config).length ? JSON.stringify(config, null, 2) : "";

  return (
    <form
      onSubmit={onSubmit}
      aria-labelledby="source-form-heading"
      className="surface flex flex-col gap-3 rounded-xl border border-line p-4"
    >
      <h3 id="source-form-heading" className="font-semibold">
        {source ? "Edit source" : "Add source"}
      </h3>
      <label className="flex flex-col gap-1 text-sm font-medium">
        URL
        <input
          name="url"
          type="url"
          required
          defaultValue={source?.url}
          placeholder="https://"
          className="field"
        />
      </label>
      <div className="flex flex-wrap gap-3">
        <label className="flex flex-col gap-1 text-sm font-medium">
          Kind
          <select
            name="kind"
            value={kind}
            onChange={(e) => setKind(e.target.value)}
            className="field"
          >
            {KINDS.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </label>
        {kind === "social" && (
          <label className="flex flex-col gap-1 text-sm font-medium">
            Network
            <select name="network" defaultValue={source?.network ?? "linkedin"} className="field">
              {NETWORKS.map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </label>
        )}
        <label className="flex flex-col gap-1 text-sm font-medium">
          Adapter
          <select
            name="adapter"
            value={adapter}
            onChange={(e) => setAdapter(e.target.value)}
            className="field"
          >
            {ADAPTERS.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        </label>
        {adapter === "api" && (
          <label className="flex flex-col gap-1 text-sm font-medium">
            Parser
            <select name="parser" defaultValue={parser} className="field">
              {PARSERS.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </label>
        )}
        <label className="flex flex-col gap-1 text-sm font-medium">
          Tier override
          <select
            name="tier_override"
            defaultValue={source?.tier_override ?? ""}
            className="field"
          >
            <option value="">Default tier</option>
            {[1, 2, 3].map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm font-medium">
          Every (hours)
          <input
            name="cadence_hours"
            type="number"
            min={1}
            max={8760}
            defaultValue={source?.cadence_hours}
            placeholder="From tier"
            aria-describedby="cadence-help"
            className="field w-32"
          />
        </label>
      </div>
      <p id="cadence-help" className="text-muted text-sm">
        Leave hours blank to use the tier default: tier 1 daily, tier 2 every 3 days, tier 3
        weekly, aggregators every 4 hours.
      </p>
      {(adapter === "selector" || configText) && (
        <label className="flex flex-col gap-1 text-sm font-medium">
          Selector config (JSON)
          <textarea
            name="selector_config"
            rows={4}
            defaultValue={configText}
            className="field py-2 font-mono text-sm"
          />
        </label>
      )}
      <label className="flex min-h-11 items-center gap-2 text-sm font-medium">
        <input
          name="is_enabled"
          type="checkbox"
          defaultChecked={source?.is_enabled ?? true}
          className="size-5"
        />
        Enabled
      </label>
      <div className="flex gap-3">
        <button type="submit" disabled={busy} className="btn-primary">
          {busy ? "Saving…" : source ? "Save source" : "Add source"}
        </button>
        <button type="button" className="btn-secondary" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}
