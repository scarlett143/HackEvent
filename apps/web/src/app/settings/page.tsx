"use client";

// Settings (design brief §4.5, App Flow F8, HE-077): Profile · Timezone · Reminders · Channels ·
// Quiet hours · Export, plus password change. Each section saves on its own.
// ponytail: email + in-app channels only; Telegram / ntfy / push rows join with HE-074..076.

import { useEffect, useState } from "react";
import { api, type Me } from "@/lib/api";
import {
  type Channel,
  formatQuietHours,
  offsetChips,
  offsetLabel,
  parseOffset,
  type Prefs,
  timezoneOptions,
  toggleOffset,
} from "@/lib/settings";
import { loadEvents } from "@/lib/load-events";
import { matchSettings, type SectionId, SUGGESTIONS } from "@/lib/settings-search";
import { downloadIcs, downloadXls } from "../calendar-menu";
import { Icon } from "../symbol";
import { AppShell, Avatar, useShell } from "../ui";
import { playChime, setSoundEnabled, soundEnabled } from "@/lib/sound";

const MIN_PASSWORD = 12;

export default function SettingsPage() {
  return (
    <AppShell>
      <Settings />
    </AppShell>
  );
}

function Section({ id, title, children }: { id: string; title: string; children: React.ReactNode }) {
  return (
    <section aria-labelledby={id} className="card flex flex-col gap-3 p-4">
      <h2 id={id} className="font-display text-xl font-semibold">
        {title}
      </h2>
      {children}
    </section>
  );
}

function Settings() {
  const { me, setMe, toast, refreshUnread } = useShell();
  const [prefs, setPrefs] = useState<Prefs | null>(null);
  const [loadError, setLoadError] = useState(false);

  useEffect(() => {
    api<Prefs>("/api/v1/me/notification-prefs").then((r) => (r.ok ? setPrefs(r.data) : setLoadError(true)));
  }, []);

  /** PATCH /me with some fields; toast the outcome. */
  async function saveMe(body: Partial<Me>, done: string): Promise<boolean> {
    const r = await api<Me>("/api/v1/me", "PATCH", body);
    if (!r.ok) {
      toast({ message: r.status === 422 ? "Check the values and try again." : "Could not save. Try again." });
      return false;
    }
    setMe(r.data);
    toast({ message: done });
    return true;
  }

  async function savePrefs(next: Prefs, done: string): Promise<boolean> {
    const r = await api<Prefs>("/api/v1/me/notification-prefs", "PUT", next);
    if (!r.ok) {
      toast({ message: "Could not save. Try again." });
      return false;
    }
    setPrefs(r.data);
    toast({ message: done });
    return true;
  }

  const [q, setQ] = useState("");
  const shownIds = matchSettings(q);
  const vis = (id: SectionId) => shownIds.has(id);

  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-6 p-4 md:p-6">
      <h1 className="font-display text-[1.75rem] leading-8 font-bold">Settings</h1>
      <ProfileCard me={me} />
      <div className="flex flex-col gap-2">
        <label className="sr-only" htmlFor="settings-q">
          Search settings
        </label>
        <input
          id="settings-q"
          type="search"
          placeholder="Search settings"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          className="field"
        />
        <div role="group" aria-label="Quick suggestions" className="flex flex-wrap items-center gap-2">
          <span className="text-muted text-sm">Try:</span>
          {SUGGESTIONS.map((sug) => (
            <button key={sug} type="button" className="chip focus-ring min-h-11 px-3 text-sm" aria-pressed={q === sug} onClick={() => setQ(q === sug ? "" : sug)}>
              {sug}
            </button>
          ))}
        </div>
        <p className="text-muted text-sm" aria-live="polite">
          {q.trim() ? `${shownIds.size} matching ${shownIds.size === 1 ? "setting" : "settings"}` : ""}
        </p>
        {shownIds.size === 0 && (
          <p className="card p-4">
            No settings match &ldquo;{q}&rdquo;.{" "}
            <button className="link" onClick={() => setQ("")}>
              Clear search
            </button>
          </p>
        )}
      </div>
      {vis("profile") && <ProfileSection me={me} saveMe={saveMe} />}
      {vis("password") && <PasswordSection />}
      {vis("timezone") && <TimezoneSection me={me} saveMe={saveMe} />}
      {loadError && (
        <p role="alert" className="text-urgent">
          Could not load your reminder settings. Reload to try again.
        </p>
      )}
      {prefs ? (
        <>
          {vis("reminders") && <RemindersSection prefs={prefs} savePrefs={savePrefs} />}
          {vis("channels") && <ChannelsSection me={me} prefs={prefs} savePrefs={savePrefs} onInApp={refreshUnread} />}
        </>
      ) : (
        !loadError && (vis("reminders") || vis("channels")) && <div className="skeleton h-40" aria-hidden />
      )}
      {vis("quiet") && <QuietHoursSection me={me} saveMe={saveMe} />}
      {vis("export") && <ExportSection />}
    </main>
  );
}

/** Stitch profile header: who you are + live Tracked / Registered counts from the events API. */
function ProfileCard({ me }: { me: Me }) {
  const [stats, setStats] = useState<{ tracked: number; registered: number } | null>(null);
  useEffect(() => {
    loadEvents().then(
      (ev) => ev && setStats({ tracked: ev.filter((e) => e.is_tracked).length, registered: ev.filter((e) => e.is_registered).length }),
    );
  }, []);
  return (
    <section aria-label="Your profile" className="card flex items-center gap-4 p-4">
      <Avatar name={me.name} />
      <div className="flex min-w-0 flex-1 flex-col">
        <p className="font-display text-xl font-semibold break-words">{me.name}</p>
        <p className="text-muted text-sm break-all">
          {me.email} · {me.role}
        </p>
      </div>
      <dl className="flex gap-4 text-center">
        <div>
          <dt className="text-muted text-xs">Tracked</dt>
          <dd className="font-display text-2xl font-bold tabular-nums">{stats?.tracked ?? "—"}</dd>
        </div>
        <div>
          <dt className="text-muted text-xs">Registered</dt>
          <dd className="font-display text-2xl font-bold tabular-nums">{stats?.registered ?? "—"}</dd>
        </div>
      </dl>
    </section>
  );
}

/** Export my data: everything you track or registered for, as an Excel sheet (.xls) or a calendar file (.ics). */
function ExportSection() {
  const { toast, tz } = useShell();
  const [busy, setBusy] = useState(false);

  async function run(kind: "xls" | "ics") {
    setBusy(true);
    const all = await loadEvents();
    setBusy(false);
    if (!all) return toast({ message: "Could not load your events. Try again." });
    const mine = all.filter((e) => e.is_tracked || e.is_registered);
    if (mine.length === 0) return toast({ message: "Nothing to export yet. Track or register for an event first." });
    if (kind === "ics") downloadIcs("hackevents.ics", mine);
    else downloadXls("hackevents.xls", mine, tz);
  }

  return (
    <Section id="export" title="Export my data">
      <p className="text-muted text-sm">Everything you track or registered for, as an Excel sheet or a calendar file.</p>
      <div className="flex flex-wrap gap-2">
        <button className="btn-secondary" disabled={busy} onClick={() => run("xls")}>
          Export .xls
        </button>
        <button className="btn-secondary" disabled={busy} onClick={() => run("ics")}>
          Export .ics
        </button>
      </div>
    </Section>
  );
}

type SaveMe = (body: Partial<Me>, done: string) => Promise<boolean>;
type SavePrefs = (next: Prefs, done: string) => Promise<boolean>;

function ProfileSection({ me, saveMe }: { me: Me; saveMe: SaveMe }) {
  const [name, setName] = useState(me.name);
  const [busy, setBusy] = useState(false);
  return (
    <Section id="profile" title="Profile">
      <form
        className="flex flex-col gap-3"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          await saveMe({ name }, "Name saved.");
          setBusy(false);
        }}
      >
        <label className="flex flex-col gap-1 text-sm font-medium">
          Name
          <input
            className="field"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            maxLength={100}
            autoComplete="name"
          />
        </label>
        <p className="text-muted text-sm">
          Email: <span className="break-all">{me.email}</span> · Role: {me.role}
        </p>
        <button className="btn-primary self-start" disabled={busy || !name.trim() || name === me.name}>
          {busy ? "Saving…" : "Save name"}
        </button>
      </form>
    </Section>
  );
}

function PasswordSection() {
  const { toast } = useShell();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const f = new FormData(form);
    const next = String(f.get("new_password"));
    setError(null);
    if (next !== f.get("confirm")) return setError("The new passwords do not match.");
    if (next.length < MIN_PASSWORD) return setError(`Use at least ${MIN_PASSWORD} characters.`);
    setBusy(true);
    const r = await api("/api/v1/me/password", "POST", {
      current_password: f.get("current_password"),
      new_password: next,
    });
    setBusy(false);
    if (!r.ok) return setError(r.status === 429 ? "Too many attempts. Try again later." : r.error.message);
    form.reset();
    toast({ message: "Password changed. Other devices are signed out." });
  }

  return (
    <Section id="password" title="Password">
      <form className="flex flex-col gap-3" onSubmit={submit} aria-describedby="pw-policy">
        <p id="pw-policy" className="text-muted text-sm">
          At least {MIN_PASSWORD} characters. Common passwords are refused. Changing it signs out your
          other devices.
        </p>
        <label className="flex flex-col gap-1 text-sm font-medium">
          Current password
          <input className="field" name="current_password" type="password" autoComplete="current-password" required />
        </label>
        <label className="flex flex-col gap-1 text-sm font-medium">
          New password
          <input
            className="field"
            name="new_password"
            type="password"
            autoComplete="new-password"
            minLength={MIN_PASSWORD}
            maxLength={128}
            required
          />
        </label>
        <label className="flex flex-col gap-1 text-sm font-medium">
          Confirm new password
          <input className="field" name="confirm" type="password" autoComplete="new-password" required />
        </label>
        {error && (
          <p role="alert" className="text-urgent text-sm">
            {error}
          </p>
        )}
        <button className="btn-primary self-start" disabled={busy}>
          {busy ? "Changing…" : "Change password"}
        </button>
      </form>
    </Section>
  );
}

function TimezoneSection({ me, saveMe }: { me: Me; saveMe: SaveMe }) {
  const [tz, setTz] = useState(me.timezone);
  const [zones] = useState(() => timezoneOptions(me.timezone));
  return (
    <Section id="timezone" title="Timezone">
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          void saveMe({ timezone: tz }, `Timezone set to ${tz}.`);
        }}
      >
        <label className="flex min-w-64 flex-col gap-1 text-sm font-medium">
          Show times and send reminders in
          <select className="field" value={tz} onChange={(e) => setTz(e.target.value)}>
            {zones.map((z) => (
              <option key={z} value={z}>
                {z}
              </option>
            ))}
          </select>
        </label>
        <button className="btn-primary" disabled={tz === me.timezone}>
          Save timezone
        </button>
      </form>
    </Section>
  );
}

function OffsetPicker({
  label,
  days,
  onChange,
}: {
  label: string;
  days: number[];
  onChange: (days: number[]) => void;
}) {
  const [custom, setCustom] = useState("");
  const [error, setError] = useState<string | null>(null);
  const id = label.replace(/\W+/g, "-").toLowerCase();
  function add() {
    const d = parseOffset(custom);
    if (d === null) return setError("Enter whole days from 0 to 30.");
    setError(null);
    setCustom("");
    if (!days.includes(d)) onChange(toggleOffset(days, d));
  }
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="mb-1 font-medium">{label}</legend>
      <div className="flex flex-wrap gap-2">
        {offsetChips(days).map((d) => (
          <button
            key={d}
            type="button"
            aria-pressed={days.includes(d)}
            onClick={() => onChange(toggleOffset(days, d))}
            className={`focus-ring min-h-11 rounded-full border px-4 text-sm font-medium ${
              days.includes(d) ? "border-primary bg-primary text-on-primary" : "border-line"
            }`}
          >
            {days.includes(d) && <Icon name="check" size={16} className="mr-1 align-[-3px]" />}
            {offsetLabel(d)}
          </button>
        ))}
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col gap-1 text-sm" htmlFor={`${id}-custom`}>
          Custom (days before)
          <input
            id={`${id}-custom`}
            className="field w-32"
            inputMode="numeric"
            value={custom}
            onChange={(e) => setCustom(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                add();
              }
            }}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? `${id}-err` : undefined}
          />
        </label>
        <button type="button" className="btn-secondary" onClick={add}>
          + Custom
        </button>
      </div>
      {error && (
        <p id={`${id}-err`} role="alert" className="text-urgent text-sm">
          {error}
        </p>
      )}
    </fieldset>
  );
}

function RemindersSection({ prefs, savePrefs }: { prefs: Prefs; savePrefs: SavePrefs }) {
  const [deadline, setDeadline] = useState(prefs.deadline_offsets_days);
  const [event, setEvent] = useState(prefs.event_offsets_days);
  const dirty =
    deadline.join() !== prefs.deadline_offsets_days.join() || event.join() !== prefs.event_offsets_days.join();
  return (
    <Section id="reminders" title="Reminders">
      <p className="text-muted text-sm">Pick how many days before to remind you. Reminders go to the channels below.</p>
      <OffsetPicker label="Before the registration deadline" days={deadline} onChange={setDeadline} />
      <OffsetPicker label="Before the event (once registered)" days={event} onChange={setEvent} />
      <button
        className="btn-primary self-start"
        disabled={!dirty}
        onClick={() =>
          void savePrefs({ ...prefs, deadline_offsets_days: deadline, event_offsets_days: event }, "Reminders saved.")
        }
      >
        Save reminders
      </button>
    </Section>
  );
}

const CHANNELS: { key: Channel; label: string; status: (me: Me) => string; done: string }[] = [
  { key: "email", label: "Email", status: (me) => `Sends to ${me.email}`, done: "Test email queued. Check your inbox." },
  { key: "in_app", label: "In-app", status: () => "Shows in the bell in this app", done: "Test notification added to the bell." },
];

function ChannelsSection({
  me,
  prefs,
  savePrefs,
  onInApp,
}: {
  me: Me;
  prefs: Prefs;
  savePrefs: SavePrefs;
  onInApp: () => void;
}) {
  const { toast } = useShell();
  const [sending, setSending] = useState<Channel | null>(null);

  async function sendTest(c: (typeof CHANNELS)[number]) {
    setSending(c.key);
    const r = await api(`/api/v1/channels/${c.key}/test`, "POST");
    setSending(null);
    if (r.ok) {
      toast({ message: c.done });
      if (c.key === "in_app") onInApp();
    } else
      toast({
        message: r.status === 429 ? "Test limit reached (5 per hour). Try again later." : "Could not send the test. Try again.",
      });
  }

  return (
    <Section id="channels" title="Channels">
      <ul className="flex flex-col divide-y divide-line rounded-xl border border-line">
        {CHANNELS.map((c) => {
          const on = prefs.channels[c.key] !== false;
          return (
            <li key={c.key} className="flex flex-wrap items-center justify-between gap-3 p-4">
              <div className="min-w-48">
                <p className="font-medium">{c.label}</p>
                <p className="text-muted text-sm break-all">
                  {on ? "On" : "Off"} · {c.status(me)}
                </p>
              </div>
              <div className="flex items-center gap-2">
                <label className="inline-flex min-h-11 cursor-pointer items-center gap-2 px-1 text-sm font-medium">
                  <input
                    type="checkbox"
                    role="switch"
                    className="focus-ring size-5 accent-primary"
                    checked={on}
                    onChange={(e) =>
                      void savePrefs(
                        { ...prefs, channels: { ...prefs.channels, [c.key]: e.target.checked } },
                        `${c.label} reminders ${e.target.checked ? "on" : "off"}.`,
                      )
                    }
                  />
                  Send reminders
                </label>
                <button className="btn-secondary" disabled={sending !== null} onClick={() => sendTest(c)}>
                  {sending === c.key ? "Sending…" : "Send test"}
                  <span className="sr-only"> ({c.label})</span>
                </button>
              </div>
            </li>
          );
        })}
      </ul>
      <SoundToggle />
    </Section>
  );
}

/** Per-browser chime for new in-app notifications (lib/sound.ts). */
function SoundToggle() {
  const [on, setOn] = useState(true);
  useEffect(() => {
    // Mount-time read of this browser's choice (localStorage is not available on the server).
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setOn(soundEnabled());
  }, []);
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-line p-4">
      <div className="min-w-48">
        <p className="font-medium">Notification sound</p>
        <p className="text-muted text-sm">Plays a short chime when a new in-app notification arrives (this browser only).</p>
      </div>
      <div className="flex items-center gap-2">
        <label className="inline-flex min-h-11 cursor-pointer items-center gap-2 px-1 text-sm font-medium">
          <input
            type="checkbox"
            role="switch"
            className="focus-ring size-5 accent-primary"
            checked={on}
            onChange={(e) => {
              setOn(e.target.checked);
              setSoundEnabled(e.target.checked);
              if (e.target.checked) playChime();
            }}
          />
          Play sound
        </label>
        <button type="button" className="btn-secondary" onClick={playChime}>
          Preview
        </button>
      </div>
    </div>
  );
}

function QuietHoursSection({ me, saveMe }: { me: Me; saveMe: SaveMe }) {
  const [on, setOn] = useState(Boolean(me.quiet_start));
  const [start, setStart] = useState(me.quiet_start ?? "22:00");
  const [end, setEnd] = useState(me.quiet_end ?? "07:00");
  const current = formatQuietHours(me.quiet_start ?? null, me.quiet_end ?? null);
  const invalid = on && (!start || !end || start === end);
  return (
    <Section id="quiet" title="Quiet hours">
      <p className="text-muted text-sm">
        No reminders between these times ({me.timezone}). Now: {current}.
      </p>
      <form
        className="flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          void saveMe(
            on ? { quiet_start: start, quiet_end: end } : { quiet_start: null, quiet_end: null },
            on ? `Quiet hours set: ${formatQuietHours(start, end)}.` : "Quiet hours off.",
          );
        }}
      >
        <label className="inline-flex min-h-11 items-center gap-2 font-medium">
          <input
            type="checkbox"
            role="switch"
            className="focus-ring size-5 accent-primary"
            checked={on}
            onChange={(e) => setOn(e.target.checked)}
          />
          Pause reminders at night
        </label>
        <div className="flex flex-wrap gap-3">
          <label className="flex flex-col gap-1 text-sm font-medium">
            From
            <input className="field" type="time" value={start} disabled={!on} onChange={(e) => setStart(e.target.value)} />
          </label>
          <label className="flex flex-col gap-1 text-sm font-medium">
            Until
            <input className="field" type="time" value={end} disabled={!on} onChange={(e) => setEnd(e.target.value)} />
          </label>
        </div>
        {invalid && (
          <p role="alert" className="text-urgent text-sm">
            Start and end must be different times.
          </p>
        )}
        <button className="btn-primary self-start" disabled={invalid}>
          Save quiet hours
        </button>
      </form>
    </Section>
  );
}
