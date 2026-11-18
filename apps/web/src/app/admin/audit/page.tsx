"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { adminRedirect, api, type ApiError } from "@/lib/api";
import { formatWhen } from "@/lib/events";
import { AppShell, useShell } from "../../ui";
import { AdminNav } from "../admin-nav";

// Audit log viewer (HE-096, doc 06 A7 `GET /admin/audit`, doc 07 §11): newest first, filter by
// actor and action, "Load more" follows next_cursor. `meta` is JSON data: shown as text only.

const HERE = "/admin/audit";
// Mirrors services/common/audit.py AuditAction; add new members here too.
const ACTIONS = [
  "auth.login_success",
  "auth.login_fail",
  "invite.create",
  "invite.accept",
  "user.role_change",
  "user.deactivate",
  "provider.add",
  "provider.edit",
  "provider.delete",
  "cap.change",
  "social_session.upload",
  "social_session.delete",
  "source.create",
  "source.edit",
  "source.delete",
  "review.decision",
  "backup.restore",
] as const;

type Entry = {
  id: string;
  at: string;
  action: string;
  actor_id: string | null;
  actor_email: string | null;
  target_type: string;
  target_id: string | null;
  ip: string;
  meta: Record<string, unknown>;
};
type Page = { items: Entry[]; next_cursor: string | null };
type User = { id: string; email: string };

export default function AuditPage() {
  return (
    <AppShell>
      <Audit />
    </AppShell>
  );
}

function Audit() {
  const router = useRouter();
  const { tz } = useShell();
  const [users, setUsers] = useState<User[]>([]);
  const [actor, setActor] = useState("");
  const [action, setAction] = useState("");
  const [entries, setEntries] = useState<Entry[] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
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
    (after: string | null) => {
      const q = new URLSearchParams();
      if (actor) q.set("actor", actor);
      if (action) q.set("action", action);
      if (after) q.set("cursor", after);
      return api<Page>(`/api/v1/admin/audit?${q}`).then((r) => {
        if (!r.ok) return fail(r);
        setError(null);
        setEntries((prev) => (after ? [...(prev ?? []), ...r.data.items] : r.data.items));
        setCursor(r.data.next_cursor);
      });
    },
    [actor, action, fail],
  );

  useEffect(() => {
    api<{ items: User[] }>("/api/v1/admin/users").then((r) => (r.ok ? setUsers(r.data.items) : fail(r)));
  }, [fail]);
  useEffect(() => {
    void load(null);
  }, [load]);

  return (
    <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col gap-6 p-4 md:p-6">
      <header className="flex flex-col gap-3 border-b border-line pb-4">
        <h1 className="font-display text-[1.75rem] leading-8 font-bold">Audit log</h1>
        <AdminNav current="audit" />
      </header>

      <div role="group" aria-label="Filters" className="flex flex-wrap items-end gap-3">
        <label className="flex min-w-64 flex-col gap-1 text-sm font-medium">
          Actor
          <select className="field" value={actor} onChange={(e) => setActor(e.target.value)}>
            <option value="">Anyone</option>
            {users.map((u) => (
              <option key={u.id} value={u.id}>
                {u.email}
              </option>
            ))}
          </select>
        </label>
        <label className="flex min-w-64 flex-col gap-1 text-sm font-medium">
          Action
          <select className="field" value={action} onChange={(e) => setAction(e.target.value)}>
            <option value="">All actions</option>
            {ACTIONS.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        </label>
      </div>

      {error && (
        <p role="alert" className="text-urgent text-sm">
          {error}
        </p>
      )}

      {!entries ? (
        <p className="text-muted">Loading…</p>
      ) : entries.length === 0 ? (
        <p className="text-muted">No audit entries match.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="surface">
              <tr>
                <th scope="col" className="p-2">When</th>
                <th scope="col" className="p-2">Actor</th>
                <th scope="col" className="p-2">Action</th>
                <th scope="col" className="p-2">Target</th>
                <th scope="col" className="p-2">IP</th>
                <th scope="col" className="p-2">Details</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e) => (
                <tr key={e.id} className="border-t border-line align-top">
                  <td className="p-2 whitespace-nowrap">{formatWhen(e.at, tz)}</td>
                  <td className="p-2 break-all">
                    {e.actor_email ?? <span className="text-muted">{e.actor_id ? "unknown" : "none / deleted"}</span>}
                  </td>
                  <th scope="row" className="p-2 font-mono font-medium">{e.action}</th>
                  <td className="p-2 break-all">
                    {e.target_type}
                    {e.target_id && <span className="text-muted font-mono"> {e.target_id}</span>}
                  </td>
                  <td className="p-2 font-mono">{e.ip}</td>
                  <td className="p-2">
                    {/* JSON as text: React escapes it, never parsed as HTML. */}
                    <pre className="max-w-md font-mono text-xs break-all whitespace-pre-wrap">
                      {JSON.stringify(e.meta)}
                    </pre>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {cursor && (
        <button className="btn-secondary self-start" disabled={busy} onClick={async () => {
            setBusy(true);
            await load(cursor);
            setBusy(false);
          }}>
          {busy ? "Loading…" : "Load more"}
        </button>
      )}
    </main>
  );
}
