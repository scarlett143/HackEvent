"use client";

// Event detail (design brief §4.2, App Flow F3 step 3): two dates with countdowns, facts,
// sources with plain-text snippets, needs-review banner, actions. HE-054 UI.

import Link from "next/link";
import { useParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { type EventDetail, type EventItem, formatStart, formatWhen } from "@/lib/events";
import { AddToCalendar } from "../../calendar-menu";
import { AppShell, Chips, Countdown, EventActions, EventMeta, OrgAvatar, SourceBlock, useNow, useShell } from "../../ui";

type State = { kind: "loading" } | { kind: "missing" } | { kind: "error"; message: string } | { kind: "ready"; ev: EventDetail };

export default function EventPage() {
  return (
    // URL hooks (useParams/usePathname) must sit under Suspense with cacheComponents.
    <Suspense fallback={<p className="text-muted p-8">Loading…</p>}>
      <AppShell>
        <Detail />
      </AppShell>
    </Suspense>
  );
}

function Detail() {
  const { id } = useParams<{ id: string }>();
  const { tz } = useShell();
  const now = useNow();
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    api<EventDetail>(`/api/v1/events/${encodeURIComponent(id)}`).then((r) =>
      setState(
        r.ok
          ? { kind: "ready", ev: r.data }
          : r.status === 404
            ? { kind: "missing" }
            : { kind: "error", message: r.error.message },
      ),
    );
  }, [id]);

  const patch = useCallback((_: string, p: Partial<EventItem>) => {
    setState((s) => (s.kind === "ready" ? { ...s, ev: { ...s.ev, ...p } } : s));
  }, []);

  if (state.kind === "loading")
    return (
      <div aria-busy="true" aria-label="Loading event" className="flex flex-col gap-3 p-6">
        <div className="skeleton h-8 w-2/3" />
        <div className="skeleton h-48" />
      </div>
    );
  if (state.kind !== "ready")
    return (
      <main className="flex flex-col items-start gap-3 p-6">
        <p role="alert">
          {state.kind === "missing" ? "This event was not found." : `Could not load this event. ${state.message}`}
        </p>
        <Link className="link" href="/">
          Back to feed
        </Link>
      </main>
    );

  const ev = state.ev;
  const o = ev.organiser;
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-6 p-4 md:p-6">
      <Link className="link self-start text-sm" href="/">
        ← Back to feed
      </Link>
      {ev.review_status === "needs_review" && (
        <p role="note" className="rounded-xl border border-warning bg-surface p-3 text-sm">
          <span className="text-warning-ink font-semibold">
            <span aria-hidden>⚠ </span>Needs review.
          </span>{" "}
          Some details were not verified yet. Check the source before relying on them.
        </p>
      )}
      <header className="card flex items-start gap-4 p-4">
        <OrgAvatar name={o.name} />
        <div className="flex min-w-0 flex-col gap-2">
          <ul className="flex flex-wrap gap-1.5" aria-label="Organiser">
            {ev.is_new && <li className="rounded bg-new px-1.5 py-0.5 text-xs font-bold text-white">NEW</li>}
            {o.tier && <li className="chip">Tier {o.tier}</li>}
            {o.kind && <li className="chip">{o.kind === "company" ? "Company" : "Institution"}</li>}
            {o.region && <li className="chip">{o.region === "india" ? "India" : "Global"}</li>}
          </ul>
          <h1 className="font-display text-3xl font-bold">{ev.title}</h1>
          <EventMeta ev={ev} />
        </div>
      </header>

      <section aria-label="Dates" className="grid gap-3 sm:grid-cols-2">
        <div className="card flex flex-col gap-1 p-4">
          <h2 className="font-display text-lg font-semibold">Registration closes</h2>
          <Countdown deadline={ev.registration_deadline} now={now} tz={tz} />
        </div>
        <div className="card flex flex-col gap-1 p-4">
          <h2 className="font-display text-lg font-semibold">Hackathon window</h2>
          <p className="text-sm">
            <span className="font-semibold" aria-live="polite">
              {formatStart(ev.event_start, ev.event_end, now, tz)}
            </span>
            {ev.event_start && (
              <span className="text-muted block tabular-nums">
                {formatWhen(ev.event_start, tz)}
                {ev.event_end && ` – ${formatWhen(ev.event_end, tz)}`}
              </span>
            )}
          </p>
        </div>
      </section>

      <div className="flex flex-wrap items-center gap-2">
        <EventActions ev={ev} onPatch={patch} />
        <AddToCalendar ev={ev} />
      </div>

      <h2 className="font-display text-xl font-semibold">Event parameters</h2>
      <dl className="card grid grid-cols-[max-content_1fr] gap-x-4 gap-y-3 p-4">
        <dt className="text-muted">Eligibility</dt>
        <dd>{ev.eligibility.length ? <Chips ev={{ ...ev, is_registered: false }} /> : "Not listed"}</dd>
        <dt className="text-muted">Mode</dt>
        <dd className="capitalize">{ev.mode === "offline" ? "In person" : ev.mode}</dd>
        <dt className="text-muted">Location</dt>
        <dd>{ev.location ?? "Not listed"}</dd>
        <dt className="text-muted">Prizes</dt>
        <dd className="whitespace-pre-line">{ev.prize ?? "Not listed"}</dd>
      </dl>

      {ev.description && (
        <section aria-labelledby="about" className="flex flex-col gap-2">
          <h2 id="about" className="text-lg font-semibold">
            About
          </h2>
          <p className="whitespace-pre-line">{ev.description}</p>
        </section>
      )}

      <section aria-labelledby="source" className="flex flex-col gap-3">
        <h2 id="source" className="text-lg font-semibold">
          Source
        </h2>
        {ev.sources.length ? (
          ev.sources.map((s, i) => <SourceBlock key={`${s.url}-${i}`} source={s} tz={tz} />)
        ) : (
          <p className="text-muted">No source recorded.</p>
        )}
      </section>
    </main>
  );
}
