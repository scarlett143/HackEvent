"use client";

import { buildIcs, googleCalendarUrl } from "@/lib/calendar";
import type { EventItem } from "@/lib/events";
import { Icon } from "./symbol";

/** Browser download of an .ics file. Revoked right after the click. */
export function downloadIcs(filename: string, events: EventItem[]) {
  const url = URL.createObjectURL(new Blob([buildIcs(events)], { type: "text/calendar;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

/** "Add to cal ▾": Google Calendar link + .ics download (Apple/Outlook). Hidden when the event has no dates. */
export function AddToCalendar({ ev }: { ev: EventItem }) {
  const google = googleCalendarUrl(ev);
  if (!google) return null;
  return (
    <details className="relative">
      <summary className="btn-secondary focus-ring inline-flex cursor-pointer list-none items-center gap-2">
        <Icon name="calendar_today" /> Add to calendar <Icon name="arrow_drop_down" />
      </summary>
      <div className="card absolute left-0 z-20 mt-2 flex w-60 flex-col gap-1 p-2 shadow-[0_8px_24px_rgba(20,23,17,0.06)]">
        <a
          className="focus-ring inline-flex min-h-11 items-center rounded-lg px-3 hover:bg-surface"
          href={google}
          target="_blank"
          rel="noopener noreferrer"
        >
          Google Calendar<span className="sr-only"> (opens in a new tab)</span>
        </a>
        <button
          type="button"
          className="focus-ring inline-flex min-h-11 items-center rounded-lg px-3 text-left hover:bg-surface"
          onClick={() => downloadIcs(`${ev.title.slice(0, 40).replace(/[^\w-]+/g, "-")}.ics`, [ev])}
        >
          Apple / Outlook (.ics)
        </button>
      </div>
    </details>
  );
}
