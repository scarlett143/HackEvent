"use client";

import { buildIcs, googleCalendarUrl } from "@/lib/calendar";
import type { EventItem } from "@/lib/events";
import { buildXls } from "@/lib/xls";
import { Icon } from "./symbol";

/** Browser download of any text file. The object URL is revoked right after the click. */
export function downloadFile(filename: string, text: string, type: string) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export const downloadIcs = (filename: string, events: EventItem[]) =>
  downloadFile(filename, buildIcs(events), "text/calendar;charset=utf-8");

/** Excel workbook (.xls); times are written in the user's timezone `tz`. */
export const downloadXls = (filename: string, events: EventItem[], tz: string) =>
  downloadFile(filename, buildXls(events, tz), "application/vnd.ms-excel");

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
