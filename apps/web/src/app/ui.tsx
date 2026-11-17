"use client";

// Signed-in shell (header, nav, toasts) and the event building blocks shared by the feed
// (HE-051) and event detail (HE-054). All event text renders as React text — never HTML.

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { api, type Me } from "@/lib/api";
import {
  DEFAULT_TZ,
  type EventItem,
  type EventSource,
  formatDate,
  formatDeadline,
  formatWhen,
  safeHttpUrl,
  type UnreadCount,
  type Urgency,
  urgency,
} from "@/lib/events";

// ── Shell ────────────────────────────────────────────────────────────────────

type Toast = { message: string; undo?: () => void };
type Shell = {
  me: Me;
  setMe: (me: Me) => void;
  tz: string;
  refreshUnread: () => void;
  toast: (t: Toast) => void;
};
const ShellContext = createContext<Shell | null>(null);

export function useShell(): Shell {
  const s = useContext(ShellContext);
  if (!s) throw new Error("useShell outside <AppShell>");
  return s;
}

// `soon` items have no page yet: shown as labels, not links (no dead links, no 404 prefetch).
// ponytail: flip `soon` off as Calendar (HE-061) and Notifications (HE-076) land.
const NAV: { href: string; label: string; soon: boolean }[] = [
  { href: "/", label: "Feed", soon: false },
  { href: "/calendar", label: "Calendar", soon: true },
  { href: "/notifications", label: "Notifications", soon: true },
  { href: "/settings", label: "Settings", soon: false },
];

const TOAST_MS = 10_000;

export function AppShell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [me, setMe] = useState<Me | null>(null);
  const [unread, setUnread] = useState<UnreadCount | null>(null);
  const [toast, setToast] = useState<Toast | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);

  const refreshUnread = useCallback(() => {
    api<UnreadCount>("/api/v1/me/unread-count").then((r) => r.ok && setUnread(r.data));
  }, []);

  useEffect(() => {
    api<Me>("/api/v1/me").then((r) => {
      if (r.ok) setMe(r.data);
      else router.replace(`/login?next=${encodeURIComponent(location.pathname + location.search)}`);
    });
    refreshUnread();
  }, [router, refreshUnread]);

  const showToast = useCallback((t: Toast) => {
    clearTimeout(timer.current);
    setToast(t);
    timer.current = setTimeout(() => setToast(null), TOAST_MS);
  }, []);
  useEffect(() => () => clearTimeout(timer.current), []);

  async function logout() {
    await api("/api/v1/auth/logout", "POST");
    router.replace("/login");
  }

  if (!me) return <p className="text-muted p-8">Loading…</p>;
  const tz = me.timezone || DEFAULT_TZ;
  // Design: admins get an Admin tab (sources + health) next to Settings.
  const nav = me.role === "admin" ? [...NAV, { href: "/admin/sources", label: "Admin", soon: false }] : NAV;
  const badge = unread ? unread.new_events + unread.notifications : 0;
  const initials = me.name
    .split(/\s+/)
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();

  return (
    <ShellContext.Provider value={{ me, setMe, tz, refreshUnread, toast: showToast }}>
      <div className="flex min-h-full flex-1 flex-col">
        <header className="flex items-center justify-between gap-4 border-b border-line bg-card px-4 py-2 md:px-6">
          <Link href="/" className="focus-ring font-display rounded text-2xl font-bold">
            HackEvents
          </Link>
          <div className="flex items-center gap-2">
            {/* ponytail: becomes a link to /notifications once that page exists (HE-076). */}
            <span
              role="img"
              className="relative inline-flex size-11 items-center justify-center rounded-xl"
              aria-label={badge ? `${badge} unread` : "No unread items"}
            >
              <span aria-hidden>🔔</span>
              {badge > 0 && (
                <span
                  aria-hidden
                  className="absolute top-1 right-0.5 min-w-5 rounded-full bg-urgent px-1 text-center text-xs font-semibold text-white tabular-nums"
                >
                  {badge > 99 ? "99+" : badge}
                </span>
              )}
            </span>
            <details className="relative">
              <summary
                className="focus-ring flex size-11 cursor-pointer list-none items-center justify-center rounded-full bg-primary text-sm font-semibold text-on-primary"
                aria-label={`Account menu for ${me.name}`}
              >
                {initials || "?"}
              </summary>
              <div className="absolute right-0 z-20 mt-2 flex w-64 flex-col gap-1 rounded-xl border border-line bg-card p-3 shadow-[0_8px_24px_rgba(20,23,17,0.06)]">
                <p className="font-medium">{me.name}</p>
                <p className="text-muted text-sm break-all">{me.email}</p>
                <p className="text-muted mb-2 text-sm">{me.role}</p>
                {me.role === "admin" && (
                  <Link className="link inline-flex min-h-11 items-center" href="/admin/users">
                    Admin: users and invites
                  </Link>
                )}
                <button className="btn-secondary" onClick={logout}>
                  Sign out
                </button>
              </div>
            </details>
          </div>
        </header>
        <div className="flex flex-1 flex-col md:flex-row">
          <nav
            aria-label="Main"
            className="fixed inset-x-0 bottom-0 z-20 flex justify-around gap-1 border-t border-line bg-card px-2 py-1 md:static md:w-56 md:shrink-0 md:flex-col md:justify-start md:border-t-0 md:border-r md:bg-transparent md:p-3"
          >
            {nav.map((n) => {
              if (n.soon)
                return (
                  <span
                    key={n.href}
                    aria-disabled="true"
                    className="text-muted inline-flex min-h-12 flex-1 items-center justify-center gap-2 rounded-lg px-1 text-xs md:flex-none md:justify-start md:px-3 md:text-sm"
                  >
                    {n.label}
                    <span className="sr-only md:not-sr-only md:rounded md:border md:border-line md:px-1.5 md:text-xs">(Soon)</span>
                  </span>
                );
              const current = n.href === "/" ? pathname === "/" : pathname.startsWith(n.href);
              return (
                <Link
                  key={n.href}
                  href={n.href}
                  aria-current={current ? "page" : undefined}
                  className={`focus-ring inline-flex min-h-12 flex-1 items-center justify-center rounded-lg px-1 text-xs font-semibold md:flex-none md:justify-start md:px-3 md:text-sm ${
                    current ? "bg-primary text-on-primary" : "hover:bg-surface"
                  }`}
                >
                  {n.label}
                </Link>
              );
            })}
          </nav>
          <div className="min-w-0 flex-1 pb-20 md:pb-0">{children}</div>
        </div>
        {/* Toasts: top-right on desktop, bottom on small screens (brief §5); always-mounted live region. */}
        <div
          role="status"
          aria-live="polite"
          className="pointer-events-none fixed inset-x-4 bottom-20 z-30 flex justify-center sm:inset-x-auto sm:top-4 sm:right-4 sm:bottom-auto"
        >
          {toast && (
            <div className="pointer-events-auto flex items-center gap-3 rounded-xl bg-ink px-4 py-2 text-sm text-white ">
              <span>{toast.message}</span>
              {toast.undo && (
                <button
                  className="focus-ring min-h-11 rounded-lg px-2 font-semibold underline underline-offset-2"
                  onClick={() => {
                    toast.undo?.();
                    setToast(null);
                  }}
                >
                  Undo
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </ShellContext.Provider>
  );
}

// ── Time ─────────────────────────────────────────────────────────────────────

/** Current time, refreshed every minute (brief §5: countdown updates every minute). */
export function useNow(): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 60_000);
    return () => clearInterval(id);
  }, []);
  return now;
}

const URGENCY_STYLE: Record<Urgency, { text: string; icon: string }> = {
  urgent: { text: "text-urgent", icon: "⚠" },
  warning: { text: "text-warning-ink", icon: "⏱" },
  ok: { text: "text-ok-ink", icon: "⏱" },
  closed: { text: "text-muted", icon: "✕" },
  none: { text: "text-muted", icon: "⏱" },
};

/** "⏱ 2 days left to register" + "Closes 15 Nov, 23:59 IST". Colour always paired with the text. */
export function Countdown({ deadline, now, tz }: { deadline: string | null; now: Date; tz: string }) {
  const s = URGENCY_STYLE[urgency(deadline, now, tz)];
  return (
    <p className="flex flex-wrap items-baseline gap-x-2 text-sm">
      <span className={`font-semibold tabular-nums ${s.text}`} aria-live="polite">
        <span aria-hidden>{s.icon} </span>
        {formatDeadline(deadline, now, tz)}
      </span>
      {deadline && <span className="text-muted tabular-nums">Closes {formatWhen(deadline, tz)}</span>}
    </p>
  );
}

// ── Event pieces ─────────────────────────────────────────────────────────────

/** Organiser initials tile (Stitch event cards): "Example Corp" -> "EC". Decorative; the name is always next to it. */
export function OrgAvatar({ name }: { name: string }) {
  const text =
    name
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((w) => w[0])
      .join("")
      .toUpperCase() || "?";
  return (
    <span
      aria-hidden
      className="font-display flex size-12 shrink-0 items-center justify-center rounded-full bg-surface text-lg font-bold"
    >
      {text}
    </span>
  );
}

const MODE: Record<string, string> = { online: "Online", offline: "In person", hybrid: "Hybrid" };

/** "Example Corp · Company · Tier 1 · Online". */
export function EventMeta({ ev }: { ev: EventItem }) {
  const o = ev.organiser;
  const parts = [
    o.name,
    o.kind && (o.kind === "company" ? "Company" : "Institution"),
    o.tier && `Tier ${o.tier}`,
    MODE[ev.mode],
  ].filter(Boolean);
  return <p className="text-muted text-sm">{parts.join(" · ")}</p>;
}

export function Chips({ ev }: { ev: EventItem }) {
  if (!ev.eligibility.length && !ev.is_registered) return null;
  return (
    <ul className="flex flex-wrap gap-1.5" aria-label="Eligibility">
      {ev.is_registered && (
        <li className="chip border-ok text-ok-ink">
          <span aria-hidden>✓&nbsp;</span>Registered
        </li>
      )}
      {ev.eligibility.map((e) => (
        <li key={e} className="chip">
          {e}
        </li>
      ))}
    </ul>
  );
}

type Flag = "is_registered" | "is_tracked";

/** Optimistic track/register toggles with a 10 s undo toast (brief §5, App Flow F4/F5). */
export function useEventFlags(ev: EventItem, onPatch: (id: string, patch: Partial<EventItem>) => void) {
  const { toast } = useShell();
  return async function flip(flag: Flag) {
    const path = `/api/v1/events/${encodeURIComponent(ev.id)}/${flag === "is_registered" ? "registration" : "track"}`;
    const set = (on: boolean) => onPatch(ev.id, { [flag]: on });
    const save = async (on: boolean) => {
      set(on);
      const r = await api(path, on ? "POST" : "DELETE");
      if (!r.ok) {
        set(!on);
        toast({ message: "Could not save that change. Try again." });
      }
      return r.ok;
    };
    const on = !ev[flag];
    if (!(await save(on))) return;
    const message =
      flag === "is_registered"
        ? on
          ? `Marked as registered: ${ev.title}`
          : `Registration removed: ${ev.title}`
        : on
          ? `Reminders on: ${ev.title}`
          : `Reminders off: ${ev.title}`;
    toast({ message, undo: () => void save(!on) });
  };
}

/** Star toggle on its own (compact rows). */
export function TrackStar({
  ev,
  onPatch,
}: {
  ev: EventItem;
  onPatch: (id: string, patch: Partial<EventItem>) => void;
}) {
  const flip = useEventFlags(ev, onPatch);
  return (
    <button
      className="btn-secondary min-w-11 px-3"
      aria-pressed={ev.is_tracked}
      aria-label={ev.is_tracked ? `Reminders on for ${ev.title} (turn off)` : `Remind me about ${ev.title}`}
      onClick={() => flip("is_tracked")}
    >
      <span aria-hidden className={ev.is_tracked ? "text-warning-ink" : ""}>
        {ev.is_tracked ? "★" : "☆"}
      </span>
    </button>
  );
}

/** Register ↗ / I registered / ☆. */
export function EventActions({
  ev,
  onPatch,
}: {
  ev: EventItem;
  onPatch: (id: string, patch: Partial<EventItem>) => void;
}) {
  const flip = useEventFlags(ev, onPatch);
  const url = safeHttpUrl(ev.registration_url);

  return (
    <div className="flex flex-wrap items-center gap-2">
      {url && (
        <a className="btn-primary inline-flex items-center" href={url} target="_blank" rel="noopener noreferrer">
          Register <span aria-hidden>&nbsp;↗</span>
          <span className="sr-only"> (opens organiser site in a new tab)</span>
        </a>
      )}
      <button className="btn-secondary" aria-pressed={ev.is_registered} onClick={() => flip("is_registered")}>
        {ev.is_registered ? "Registered ✓" : "I registered"}
      </button>
      <button
        className="btn-secondary min-w-11 px-3"
        aria-pressed={ev.is_tracked}
        aria-label={ev.is_tracked ? "Reminders on (turn off)" : "Remind me"}
        title={ev.is_tracked ? "Reminders on" : "Remind me"}
        onClick={() => flip("is_tracked")}
      >
        <span aria-hidden className={ev.is_tracked ? "text-warning-ink" : ""}>
          {ev.is_tracked ? "★" : "☆"}
        </span>
      </button>
    </div>
  );
}

/** Source block (brief §4.2): link + quoted plain-text snippet + "found on <date> via <source>". */
export function SourceBlock({ source, tz }: { source: EventSource; tz: string }) {
  const url = safeHttpUrl(source.url);
  const host = url ? new URL(url).hostname : "unknown source";
  return (
    <figure className="flex flex-col gap-2 rounded-xl border border-line p-4">
      {source.snippet && (
        <blockquote className="border-l-4 border-line pl-3 whitespace-pre-line">{source.snippet}</blockquote>
      )}
      <figcaption className="text-muted text-sm">
        Found on {formatDate(source.found_at, tz)} via{" "}
        {url ? (
          <a className="link break-all" href={url} target="_blank" rel="noopener noreferrer">
            {host}
          </a>
        ) : (
          host
        )}
      </figcaption>
    </figure>
  );
}
