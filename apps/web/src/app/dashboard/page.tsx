"use client";

// Management dashboard (App Flow F9, HE-110): open events, closing this week and who is
// registered for them, registrations by person and by organiser type. Data:
// GET /api/v1/dashboard/summary (Management + Admin; professors get 403 and no nav link).
// Titles and names are rendered as React text, never HTML.

import Link from "next/link";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { formatWhen } from "@/lib/events";
import { AppShell, useShell } from "../ui";

type Summary = {
  generated_at: string;
  open_events: number;
  closing_this_week: number;
  new_this_week: number;
  closing_soon: { id: string; title: string; organiser: string; registration_deadline: string; registered_by: string[] }[];
  registrations_by_user: { user_id: string; name: string; role: string; registrations: number }[];
  registrations_by_kind: { kind: string; registrations: number }[];
};

const KIND_LABEL: Record<string, string> = { company: "Company", institution: "Institution", unmatched: "Not matched to an organisation" };

export default function DashboardPage() {
  return (
    <AppShell>
      <Dashboard />
    </AppShell>
  );
}

function Dashboard() {
  const { tz } = useShell();
  const [data, setData] = useState<Summary | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<Summary>("/api/v1/dashboard/summary").then((r) => (r.ok ? setData(r.data) : setError(r.error.message)));
  }, []);

  return (
    <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col gap-6 p-4 md:p-6">
      <header className="border-b border-line pb-4">
        <h1 className="font-display text-[1.75rem] leading-8 font-bold">College dashboard</h1>
        <p className="text-muted text-sm">What is open and who registered, across all staff.</p>
      </header>
      {error && (
        <p role="alert" className="text-urgent">
          {error}
        </p>
      )}
      {!data && !error && <div aria-busy="true" aria-label="Loading dashboard" className="skeleton h-48" />}
      {data && (
        <>
          <dl className="grid gap-3 sm:grid-cols-3">
            <Stat label="Open events" value={data.open_events} />
            <Stat label="Closing this week" value={data.closing_this_week} />
            <Stat label="New this week" value={data.new_this_week} />
          </dl>

          <section aria-labelledby="closing-heading" className="card flex flex-col gap-3 p-4">
            <h2 id="closing-heading" className="font-display text-xl font-bold">
              Closing this week
            </h2>
            {data.closing_soon.length === 0 ? (
              <p className="text-muted text-sm">No open event closes in the next 7 days.</p>
            ) : (
              <ul className="flex flex-col divide-y divide-line">
                {data.closing_soon.map((e) => (
                  <li key={e.id} className="flex flex-wrap items-baseline justify-between gap-2 py-2">
                    <div className="min-w-0">
                      <Link href={`/events/${e.id}`} className="focus-ring font-semibold break-words hover:underline">
                        {e.title}
                      </Link>
                      <p className="text-muted text-sm">
                        {e.organiser} · closes {formatWhen(e.registration_deadline, tz)}
                      </p>
                    </div>
                    <p className="text-sm">
                      {e.registered_by.length ? `Registered: ${e.registered_by.join(", ")}` : "Nobody registered yet"}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <div className="grid gap-4 md:grid-cols-2">
            <CountTable
              title="Registrations by person"
              rows={data.registrations_by_user.map((u) => [u.user_id, u.name, u.registrations])}
              empty="No active users."
            />
            <CountTable
              title="Registrations by organiser type"
              rows={data.registrations_by_kind.map((k) => [k.kind, KIND_LABEL[k.kind] ?? k.kind, k.registrations])}
              empty="No registrations yet."
            />
          </div>
        </>
      )}
    </main>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="surface flex flex-col gap-1 rounded-lg p-3">
      <dt className="text-muted text-xs font-semibold tracking-wide uppercase">{label}</dt>
      <dd className="font-display text-3xl font-bold tabular-nums">{value.toLocaleString("en-IN")}</dd>
    </div>
  );
}

function CountTable({ title, rows, empty }: { title: string; rows: [string, string, number][]; empty: string }) {
  return (
    <section className="card flex flex-col gap-3 p-4">
      <h2 className="font-display text-xl font-bold">{title}</h2>
      {rows.length === 0 ? (
        <p className="text-muted text-sm">{empty}</p>
      ) : (
        <table className="w-full text-sm">
          <tbody>
            {rows.map(([key, label, n]) => (
              <tr key={key} className="border-t border-line first:border-t-0">
                <th scope="row" className="py-2 text-left font-normal break-words">
                  {label}
                </th>
                <td className="py-2 text-right font-semibold tabular-nums">{n}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
