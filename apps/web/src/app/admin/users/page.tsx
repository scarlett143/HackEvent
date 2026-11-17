"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { adminRedirect, api, type ApiError, type Me } from "@/lib/api";
import { AppShell } from "../../ui";
import { AdminNav } from "../admin-nav";

// App Flow F1 step 1 + doc 06 A7: users table, invite form, recent invites (HE-090).
// ponytail: plain tables; TanStack (design brief §4.6) once sorting/filtering is needed.

const HERE = "/admin/users";
const ROLES = ["professor", "management", "admin"] as const;
type Role = (typeof ROLES)[number];

type User = {
  id: string;
  email: string;
  name: string;
  role: Role;
  is_active: boolean;
  created_at: string;
};
type Invite = {
  id: string;
  email: string;
  role: Role;
  created_at: string;
  expires_at: string;
  used_at: string | null;
  status: "pending" | "used" | "expired";
};
type List<T> = { items: T[]; next_cursor: null };

export default function UsersPage() {
  const router = useRouter();
  const [me, setMe] = useState<Me | null>(null);
  const [users, setUsers] = useState<User[] | null>(null);
  const [invites, setInvites] = useState<Invite[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  /** A failed admin call: redirect for sign-in / 2FA, otherwise show its message. */
  const fail = useCallback(
    (r: { status: number; error: ApiError }) => {
      const to = adminRedirect(r, HERE);
      if (to) router.replace(to);
      else setError(r.error.message);
    },
    [router],
  );

  useEffect(() => {
    Promise.all([
      api<Me>("/api/v1/me"),
      api<List<User>>("/api/v1/admin/users"),
      api<List<Invite>>("/api/v1/admin/invites"),
    ]).then(([m, u, i]) => {
      if (!m.ok) return fail(m);
      if (!u.ok) return fail(u);
      if (!i.ok) return fail(i);
      setMe(m.data);
      setUsers(u.data.items);
      setInvites(i.data.items);
    });
  }, [fail]);

  async function change(user: User, body: Partial<Pick<User, "role" | "is_active">>) {
    setError(null);
    setNotice(null);
    const r = await api<User>(`/api/v1/admin/users/${user.id}`, "PATCH", body);
    if (!r.ok) return fail(r);
    setUsers((list) => list?.map((x) => (x.id === r.data.id ? r.data : x)) ?? null);
    setNotice(
      body.role
        ? `${user.email} is now ${body.role}.`
        : `${user.email} is ${body.is_active ? "active again" : "deactivated and signed out"}.`,
    );
  }

  async function sendInvite(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const f = new FormData(form);
    setBusy(true);
    setError(null);
    setNotice(null);
    const r = await api<{ email: string }>("/api/v1/admin/invites", "POST", {
      email: f.get("email"),
      role: f.get("role"),
    });
    setBusy(false);
    if (!r.ok && r.status === 422) return setError("Enter a valid email address.");
    if (!r.ok) return fail(r);
    form.reset();
    setNotice(`Invite sent to ${r.data.email}. The link works once, for 72 hours.`);
    const i = await api<List<Invite>>("/api/v1/admin/invites");
    if (i.ok) setInvites(i.data.items);
    else fail(i);
  }

  const when = (iso: string) =>
    new Intl.DateTimeFormat("en-IN", {
      dateStyle: "medium",
      timeStyle: "short",
      timeZone: me?.timezone,
    }).format(new Date(iso));

  return (
    <AppShell>
    <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col gap-8 p-4 md:p-6">
      <header className="flex flex-col gap-3 border-b border-line pb-4">
        <h1 className="font-display text-3xl font-bold">Users and invites</h1>
        <AdminNav current="users" />
      </header>

      <div aria-live="polite" className="min-h-5 text-sm">
        {error && (
          <p role="alert" className="text-urgent">
            {error}
          </p>
        )}
        {notice && <p>{notice}</p>}
      </div>

      <section aria-labelledby="invite-heading" className="flex flex-col gap-3">
        <h2 id="invite-heading" className="text-lg font-semibold">
          Invite
        </h2>
        <form onSubmit={sendInvite} className="flex flex-wrap items-end gap-3" noValidate>
          <label className="flex min-w-64 flex-1 flex-col gap-1 text-sm font-medium">
            Email
            <input name="email" type="email" autoComplete="off" required className="field" />
          </label>
          <label className="flex flex-col gap-1 text-sm font-medium">
            Role
            <select name="role" defaultValue="professor" className="field">
              {ROLES.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </label>
          <button type="submit" disabled={busy} className="btn-primary">
            {busy ? "Sending…" : "Send invite"}
          </button>
        </form>
      </section>

      <section aria-labelledby="users-heading" className="flex flex-col gap-3">
        <h2 id="users-heading" className="text-lg font-semibold">
          Users
        </h2>
        {!users ? (
          <p className="text-muted">Loading…</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="surface">
                <tr>
                  <th scope="col" className="p-2">Email</th>
                  <th scope="col" className="p-2">Name</th>
                  <th scope="col" className="p-2">Role</th>
                  <th scope="col" className="p-2">Status</th>
                  <th scope="col" className="p-2">Joined</th>
                  <th scope="col" className="p-2">Actions</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => {
                  const self = u.id === me?.id;
                  return (
                    <tr key={u.id} className="border-t border-line">
                      <th scope="row" className="p-2 font-medium">
                        {u.email}
                        {self && <span className="text-muted font-normal"> (you)</span>}
                      </th>
                      <td className="p-2">{u.name}</td>
                      <td className="p-2">
                        <select
                          aria-label={`Role for ${u.email}`}
                          value={u.role}
                          disabled={self}
                          onChange={(e) => change(u, { role: e.target.value as Role })}
                          className="field"
                        >
                          {ROLES.map((r) => (
                            <option key={r} value={r}>
                              {r}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td className={`p-2 ${u.is_active ? "" : "text-muted"}`}>
                        {u.is_active ? "Active" : "Deactivated"}
                      </td>
                      <td className="p-2">{when(u.created_at)}</td>
                      <td className="p-2">
                        {!self && (
                          <button
                            className="btn-secondary"
                            aria-label={`${u.is_active ? "Deactivate" : "Reactivate"} ${u.email}`}
                            onClick={() => change(u, { is_active: !u.is_active })}
                          >
                            {u.is_active ? "Deactivate" : "Reactivate"}
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section aria-labelledby="invites-heading" className="flex flex-col gap-3">
        <h2 id="invites-heading" className="text-lg font-semibold">
          Invites (pending and last 30 days)
        </h2>
        {invites.length === 0 ? (
          <p className="text-muted">No invites yet.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="surface">
                <tr>
                  <th scope="col" className="p-2">Email</th>
                  <th scope="col" className="p-2">Role</th>
                  <th scope="col" className="p-2">Status</th>
                  <th scope="col" className="p-2">Sent</th>
                  <th scope="col" className="p-2">Expires</th>
                </tr>
              </thead>
              <tbody>
                {invites.map((i) => (
                  <tr key={i.id} className="border-t border-line">
                    <th scope="row" className="p-2 font-medium">
                      {i.email}
                    </th>
                    <td className="p-2">{i.role}</td>
                    <td className={`p-2 ${i.status === "expired" ? "text-muted" : ""}`}>
                      {i.used_at
                        ? `Accepted ${when(i.used_at)}`
                        : i.status === "pending"
                          ? "Pending"
                          : "Expired"}
                    </td>
                    <td className="p-2">{when(i.created_at)}</td>
                    <td className="p-2">{when(i.expires_at)}</td>
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
