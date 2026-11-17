"use client";

// Event detail (design brief §4.2, App Flow F3 step 3): two dates with countdowns, facts,
// sources with plain-text snippets, needs-review banner, actions. HE-054 UI.

import Link from "next/link";
import { useParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { daysBetween, type EventDetail, type EventItem, formatDeadline, formatStart, formatWhen, tMinus, urgency } from "@/lib/events";
import { AddToCalendar } from "../../calendar-menu";
import { Icon } from "../../symbol";
import { AppShell, Chips, Avatar, EventActions, EventMeta, SourceBlock, useNow, useShell } from "../../ui";

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
  const u = urgency(ev.registration_deadline, now, tz);
  const daysLeft = ev.registration_deadline && u !== "closed" ? daysBetween(now, new Date(ev.registration_deadline), tz) : null;
  const toStart = ev.event_start && new Date(ev.event_start) > now ? daysBetween(now, new Date(ev.event_start), tz) : null;
  const sprint = ev.event_start && ev.event_end ? daysBetween(new Date(ev.event_start), new Date(ev.event_end), tz) + 1 : null;
  const tm = tMinus(ev.registration_deadline, now);
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-6 p-4 md:p-6">
      <Link className="link inline-flex min-h-11 items-center gap-1 self-start text-sm" href="/">
        <Icon name="arrow_back_ios_new" size={16} />
        Back to feed
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
        <Avatar name={ev.title} />
        <div className="flex min-w-0 flex-col gap-2">
          {ev.is_new && (
            <span className="w-fit rounded bg-new px-1.5 py-0.5 text-xs font-bold text-white">NEW</span>
          )}
          <h1 className="font-display text-3xl font-bold">{ev.title}</h1>
          <EventMeta ev={ev} />
        </div>
      </header>

      <section
        aria-label="Registration status"
        className={`flex items-center justify-between gap-3 rounded-xl border p-4 ${STRIP[u].box}`}
      >
        <div className="flex min-w-0 items-center gap-3">
          <Icon name={STRIP[u].icon} size={24} />
          <div className="min-w-0">
            <p className="font-display text-lg font-semibold" aria-live="polite">
              {formatDeadline(ev.registration_deadline, now, tz)}
            </p>
            {ev.registration_deadline && (
              <p className="text-sm tabular-nums">Closes {formatWhen(ev.registration_deadline, tz)}</p>
            )}
          </div>
        </div>
        {STRIP[u].label && <span className="chip shrink-0 font-semibold">{STRIP[u].label}</span>}
      </section>

      <dl className="grid grid-cols-3 gap-3 text-center">
        <Tile label="Days left" value={daysLeft === null ? "—" : String(daysLeft)} unit={daysLeft === null ? "" : "d"} />
        <Tile label="To start" value={toStart === null ? "—" : String(toStart)} unit={toStart === null ? "" : "d"} />
        <Tile label="Duration" value={sprint === null ? "—" : String(sprint)} unit={sprint === null ? "" : sprint === 1 ? "day" : "days"} />
      </dl>

      <div className="flex flex-wrap items-center gap-2">
        <EventActions ev={ev} onPatch={patch} variant="detail" />
        <AddToCalendar ev={ev} />
      </div>

      <h2 className="font-display text-xl font-semibold">Critical timeline</h2>
      <section aria-label="Critical timeline" className="flex flex-col gap-3">
        <div className="card flex gap-3 p-4">
          <span aria-hidden className="flex size-11 shrink-0 items-center justify-center rounded-lg bg-urgent/10 text-urgent">
            <Icon name="hourglass_bottom" size={24} />
          </span>
          <div className="flex min-w-0 flex-1 flex-col gap-0.5">
            <div className="flex items-baseline justify-between gap-2">
              <p className="text-xs font-semibold tracking-wide text-urgent uppercase">Registration closes</p>
              {tm && <p className="text-urgent text-sm font-semibold tabular-nums">{tm}</p>}
            </div>
            <p className="font-display text-xl font-semibold tabular-nums">
              {ev.registration_deadline ? formatWhen(ev.registration_deadline, tz) : "Not listed"}
            </p>
            <p className="text-muted text-sm">Times shown in {tz}.</p>
          </div>
        </div>
        <div className="card flex gap-3 p-4">
          <span aria-hidden className="flex size-11 shrink-0 items-center justify-center rounded-lg bg-teal/15 text-ok-ink">
            <Icon name="bolt" size={24} />
          </span>
          <div className="flex min-w-0 flex-1 flex-col gap-0.5">
            <div className="flex items-baseline justify-between gap-2">
              <p className="text-ok-ink text-xs font-semibold tracking-wide uppercase">Hackathon window</p>
              {sprint !== null && <span className="chip">{sprint}-day sprint</span>}
            </div>
            <p className="font-display text-xl font-semibold tabular-nums" aria-live="polite">
              {ev.event_start
                ? `${formatWhen(ev.event_start, tz)}${ev.event_end ? ` – ${formatWhen(ev.event_end, tz)}` : ""}`
                : "Dates not listed"}
            </p>
            <p className="text-muted text-sm">{formatStart(ev.event_start, ev.event_end, now, tz)}</p>
          </div>
        </div>
      </section>

      <h2 className="font-display text-xl font-semibold">Event parameters</h2>
      <dl className="card flex flex-col px-4">
        <Row icon="event_busy" label="Registration closes">
          {ev.registration_deadline ? <span className="text-urgent font-semibold">{formatWhen(ev.registration_deadline, tz)}</span> : "Not listed"}
        </Row>
        <Row icon="date_range" label="Event dates">
          {ev.event_start ? `${formatWhen(ev.event_start, tz)}${ev.event_end ? ` – ${formatWhen(ev.event_end, tz)}` : ""}` : "Not listed"}
        </Row>
        <Row icon="school" label="Eligibility">
          {ev.eligibility.length ? <Chips ev={{ ...ev, is_registered: false }} /> : "Not listed"}
        </Row>
        <Row icon="devices" label="Mode">
          <span className="capitalize">{ev.mode === "offline" ? "In person" : ev.mode}</span>
        </Row>
        <Row icon="public" label="Location">
          {ev.location ?? "Not listed"}
        </Row>
        <Row icon="emoji_events" label="Prizes" last>
          <span className="text-ok-ink font-semibold whitespace-pre-line">{ev.prize ?? "Not listed"}</span>
        </Row>
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

// Status strip under the hero: urgency tint + icon; the chip text always carries the meaning.
const STRIP = {
  urgent: { box: "border-urgent/30 bg-urgent/10", icon: "alarm", label: "Urgent" },
  warning: { box: "border-warning/30 bg-warning/10", icon: "schedule", label: "Closing soon" },
  ok: { box: "border-ok/30 bg-ok/10", icon: "check_circle", label: "Open" },
  closed: { box: "border-line bg-surface", icon: "cancel", label: "Closed" },
  none: { box: "border-line bg-surface", icon: "timer", label: "" },
} as const;

function Tile({ label, value, unit }: { label: string; value: string; unit: string }) {
  return (
    <div className="card flex flex-col items-center gap-0.5 p-3">
      <dd className="font-display text-3xl font-bold tabular-nums">
        {value}
        {unit && <span className="text-muted ml-1 text-base font-semibold">{unit}</span>}
      </dd>
      <dt className="text-muted text-xs">{label}</dt>
    </div>
  );
}

function Row({ icon, label, last = false, children }: { icon: string; label: string; last?: boolean; children: React.ReactNode }) {
  return (
    <div className={`flex items-center justify-between gap-4 py-3 ${last ? "" : "border-b border-line"}`}>
      <dt className="text-muted flex items-center gap-3 text-sm">
        <Icon name={icon} />
        {label}
      </dt>
      <dd className="min-w-0 text-right text-sm">{children}</dd>
    </div>
  );
}
