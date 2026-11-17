"use client";

// Signed-in shell (header, nav, toasts) and the event building blocks shared by the feed
// (HE-051) and event detail (HE-054). All event text renders as React text — never HTML.

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Icon } from "./symbol";
import { ThemeSwitch } from "./theme";
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
type NavItem = { href: string; label: string; icon: string; soon: boolean };
const NAV: NavItem[] = [
  { href: "/", label: "Feed", icon: "grid_view", soon: false },
  { href: "/calendar", label: "Calendar", icon: "calendar_month", soon: false },
  { href: "/notifications", label: "Alerts", icon: "notifications", soon: false },
  { href: "/settings", label: "Settings", icon: "settings", soon: false },
];

/** Header subtitle under the logo (Stitch shows the current screen's name). */
function screenName(path: string): string {
  if (path === "/") return "Feed";
  if (path.startsWith("/events")) return "Event details";
  if (path.startsWith("/calendar")) return "Calendar";
  if (path.startsWith("/notifications")) return "Alerts";
  if (path.startsWith("/settings")) return "Settings";
  if (path.startsWith("/admin")) return "Admin console";
  return "";
}

const TOAST_MS = 10_000;
const LIVE_MS = 60_000; // how often the bell refreshes while the tab is visible

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

  // Live bell: refresh every minute while visible, and as soon as the tab is shown again.
  useEffect(() => {
    const tick = () => {
      if (document.visibilityState === "visible") refreshUnread();
    };
    const id = setInterval(tick, LIVE_MS);
    document.addEventListener("visibilitychange", tick);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [refreshUnread]);

  // Toast when the unread total goes UP (never on first load or when it drops).
  const total = unread ? unread.new_events + unread.notifications : null;
  const lastTotal = useRef<number | null>(null);
  useEffect(() => {
    if (total === null) return;
    const prev = lastTotal.current;
    lastTotal.current = total;
    if (prev !== null && total > prev)
      showToast({ message: `${total - prev} new ${total - prev === 1 ? "notification" : "notifications"}` });
  }, [total, showToast]);

  if (!me) return <p className="text-muted p-8">Loading…</p>;
  const tz = me.timezone || DEFAULT_TZ;
  // Design: admins get an Admin tab (sources + health) next to Settings.
  const nav: NavItem[] = me.role === "admin" ? [...NAV, { href: "/admin/sources", label: "Admin", icon: "database", soon: false }] : NAV;
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
          <Link href="/" className="focus-ring flex items-center gap-2 rounded">
            <span aria-hidden className="flex size-9 items-center justify-center rounded-lg bg-primary text-on-primary">
              <Icon name="bolt" fill size={22} />
            </span>
            <span className="flex flex-col leading-tight">
              <span className="font-display text-xl font-bold">HackEvents</span>
              <span className="text-muted text-xs">{screenName(pathname)}</span>
            </span>
          </Link>
          <div className="flex items-center gap-2">
            <Link
              href="/notifications"
              className="focus-ring relative inline-flex size-11 items-center justify-center rounded-xl"
              aria-label={badge ? `Notifications, ${badge} unread` : "Notifications"}
            >
              <Icon name="notifications" size={24} />
              {badge > 0 && (
                <span
                  aria-hidden
                  className="absolute top-1 right-0.5 min-w-5 rounded-full bg-urgent px-1 text-center text-xs font-semibold text-white tabular-nums"
                >
                  {badge > 99 ? "99+" : badge}
                </span>
              )}
            </Link>
            <details className="relative md:hidden">
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
                <div className="my-2 border-t border-line pt-3">
                  <ThemeSwitch compact />
                </div>
                <button className="btn-secondary inline-flex items-center justify-center gap-2" onClick={logout}>
                  <Icon name="logout" />
                  Sign out
                </button>
              </div>
            </details>
          </div>
        </header>
        <div className="flex flex-1 flex-col md:flex-row">
          <aside className="md:sticky md:top-0 md:flex md:h-[calc(100dvh-3.8rem)] md:w-56 md:shrink-0 md:flex-col md:self-start md:border-r md:border-line">
          <nav
            aria-label="Main"
            className="fixed inset-x-0 bottom-0 z-20 flex justify-around gap-1 border-t border-line bg-card px-2 py-1 md:static md:flex-col md:justify-start md:border-t-0 md:bg-transparent md:p-3"
          >
            {nav.map((n) => {
              if (n.soon)
                return (
                  <span
                    key={n.href}
                    aria-disabled="true"
                    className="text-muted inline-flex min-h-12 flex-1 items-center justify-center gap-2 rounded-lg px-1 text-xs md:flex-none md:justify-start md:px-3 md:text-sm"
                  >
                    <Icon name={n.icon} />
                    {n.label}
                    <span className="sr-only md:not-sr-only md:rounded md:border md:border-line md:px-1.5 md:text-xs">(Soon)</span>
                  </span>
                );
              const base = n.href.startsWith("/admin") ? "/admin" : n.href; // every admin page highlights Admin
              const current = base === "/" ? pathname === "/" : pathname.startsWith(base);
              return (
                <Link
                  key={n.href}
                  href={n.href}
                  aria-current={current ? "page" : undefined}
                  className={`focus-ring relative inline-flex min-h-14 flex-1 flex-col items-center justify-center gap-0.5 rounded-xl px-1 text-xs font-semibold md:min-h-12 md:flex-none md:flex-row md:justify-start md:gap-3 md:px-3 md:text-sm ${
                    current ? "bg-primary text-on-primary" : "hover:bg-surface"
                  }`}
                >
                  <Icon name={n.icon} fill={current} size={22} />
                  {n.label}
                  {n.href === "/notifications" && badge > 0 && (
                    <span
                      aria-label={`${badge} unread`}
                      className="absolute top-1 right-3 min-w-5 rounded-full bg-urgent px-1 text-center text-xs font-semibold text-white tabular-nums md:static md:ml-auto"
                    >
                      {badge > 99 ? "99+" : badge}
                    </span>
                  )}
                </Link>
              );
            })}
          </nav>
          {/* Desktop only (phones use the avatar menu in the header): theme, who you are, sign out. */}
          <div className="mt-auto hidden flex-col gap-3 border-t border-line p-3 md:flex">
            <ThemeSwitch compact />
            <div className="flex items-center gap-3">
              <span
                aria-hidden
                className="flex size-10 shrink-0 items-center justify-center rounded-full bg-primary text-sm font-semibold text-on-primary"
              >
                {initials || "?"}
              </span>
              <div className="min-w-0">
                <p className="truncate font-medium">{me.name}</p>
                <p className="text-muted truncate text-xs capitalize">{me.role}</p>
              </div>
            </div>
            <button className="btn-secondary inline-flex items-center justify-center gap-2" onClick={logout}>
              <Icon name="logout" />
              Sign out
            </button>
          </div>
          </aside>
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
  urgent: { text: "text-urgent", icon: "alarm" },
  warning: { text: "text-warning-ink", icon: "schedule" },
  ok: { text: "text-ok-ink", icon: "check_circle" },
  closed: { text: "text-muted", icon: "cancel" },
  none: { text: "text-muted", icon: "timer" },
};

/** "2 days left to register" + "Closes 15 Nov, 23:59 IST", with a status icon. Colour always paired with the text. */
export function Countdown({ deadline, now, tz }: { deadline: string | null; now: Date; tz: string }) {
  const s = URGENCY_STYLE[urgency(deadline, now, tz)];
  return (
    <p className="flex flex-wrap items-baseline gap-x-2 text-sm">
      <span className={`inline-flex items-center gap-1.5 font-semibold tabular-nums ${s.text}`} aria-live="polite">
        <Icon name={s.icon} size={18} />
        {formatDeadline(deadline, now, tz)}
      </span>
      {deadline && <span className="text-muted tabular-nums">Closes {formatWhen(deadline, tz)}</span>}
    </p>
  );
}

// ── Event pieces ─────────────────────────────────────────────────────────────

/** Initials tile (Stitch cards): an event title or a person's name -> "EC". Decorative; the name is always next to it. */
export function Avatar({ name }: { name: string }) {
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

/** "Online" / "In person · Mumbai". Organisation details are intentionally not shown. */
export function EventMeta({ ev }: { ev: EventItem }) {
  const parts = [MODE[ev.mode], ev.location].filter(Boolean);
  return parts.length ? <p className="text-muted text-sm">{parts.join(" · ")}</p> : null;
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
  ghost = false,
}: {
  ev: EventItem;
  onPatch: (id: string, patch: Partial<EventItem>) => void;
  /** Plain icon button (card header) instead of the bordered secondary button. */
  ghost?: boolean;
}) {
  const flip = useEventFlags(ev, onPatch);
  return (
    <button
      className={ghost ? "focus-ring inline-flex size-11 shrink-0 items-center justify-center rounded-full hover:bg-surface" : "btn-secondary min-w-11 px-3"}
      aria-pressed={ev.is_tracked}
      aria-label={ev.is_tracked ? `Reminders on for ${ev.title} (turn off)` : `Remind me about ${ev.title}`}
      onClick={() => flip("is_tracked")}
    >
      <Icon name="star" fill={ev.is_tracked} className={ev.is_tracked ? "text-warning-ink" : ""} />
    </button>
  );
}

/** Register ↗ / I registered / ☆. */
export function EventActions({
  ev,
  onPatch,
  variant = "card",
}: {
  ev: EventItem;
  onPatch: (id: string, patch: Partial<EventItem>) => void;
  /** "card": Register + I registered (the star lives top-right of the card). "detail": adds a labelled Remind me. */
  variant?: "card" | "detail";
}) {
  const flip = useEventFlags(ev, onPatch);
  const url = safeHttpUrl(ev.registration_url);

  return (
    <div className="flex flex-wrap items-center gap-2">
      {url && (
        <a className="btn-primary inline-flex flex-1 items-center justify-center gap-2" href={url} target="_blank" rel="noopener noreferrer">
          Register <Icon name="arrow_outward" />
          <span className="sr-only"> (opens the registration page in a new tab)</span>
        </a>
      )}
      <button
        className="btn-secondary inline-flex items-center justify-center gap-2"
        aria-pressed={ev.is_registered}
        onClick={() => flip("is_registered")}
      >
        <Icon name="check" />
        {ev.is_registered ? "Registered" : "I registered"}
      </button>
      {variant === "detail" && (
        <button
          className="btn-secondary inline-flex items-center justify-center gap-2"
          aria-pressed={ev.is_tracked}
          onClick={() => flip("is_tracked")}
        >
          <Icon name="alarm" fill={ev.is_tracked} />
          {ev.is_tracked ? "Reminders on" : "Remind me"}
        </button>
      )}
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
