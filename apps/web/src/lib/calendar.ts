// Calendar export (Stitch "Add to cal" + "Export .ics"): pure builders, no network, no OAuth.
// Scraped text is untrusted: it is escaped for iCalendar and URL-encoded for Google, never evaluated.

import type { EventItem } from "./events";

type Item = Pick<EventItem, "id" | "title" | "registration_deadline" | "event_start" | "event_end" | "location">;

const HOUR = 3_600_000;

/** 20261119T181500Z */
export function icsStamp(iso: string): string {
  return new Date(iso).toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");
}

/** RFC 5545 §3.3.11 TEXT escaping. */
export function icsEscape(s: string): string {
  return s.replace(/\\/g, "\\\\").replace(/;/g, "\\;").replace(/,/g, "\\,").replace(/\r?\n/g, "\\n");
}

/** RFC 5545 §3.1: lines over 75 octets are folded with CRLF + one space. Splits on characters, not bytes. */
export function icsFold(line: string): string {
  const enc = new TextEncoder();
  const out: string[] = [];
  let cur = "";
  let bytes = 0;
  for (const ch of line) {
    const n = enc.encode(ch).length;
    if (bytes + n > (out.length ? 74 : 75)) {
      out.push(cur);
      cur = "";
      bytes = 0;
    }
    cur += ch;
    bytes += n;
  }
  out.push(cur);
  return out.join("\r\n ");
}

type Slot = { uid: string; summary: string; start: string; end: string; location: string | null };

/** The calendar entries for one event: its deadline (30 min) and its run (start..end, default 1 h). */
export function slots(ev: Item): Slot[] {
  const out: Slot[] = [];
  if (ev.registration_deadline) {
    const d = new Date(ev.registration_deadline).getTime();
    out.push({
      uid: `${ev.id}-deadline`,
      summary: `Registration closes: ${ev.title}`,
      start: new Date(d - 0.5 * HOUR).toISOString(),
      end: new Date(d).toISOString(),
      location: null,
    });
  }
  if (ev.event_start) {
    const s = new Date(ev.event_start).getTime();
    const e = ev.event_end && new Date(ev.event_end).getTime() > s ? new Date(ev.event_end).getTime() : s + HOUR;
    out.push({
      uid: `${ev.id}-event`,
      summary: ev.title,
      start: new Date(s).toISOString(),
      end: new Date(e).toISOString(),
      location: ev.location,
    });
  }
  return out;
}

/** A complete .ics file for any number of events. `now` is injectable for tests. */
export function buildIcs(events: Item[], now = new Date()): string {
  const lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//HackEvents//EN", "CALSCALE:GREGORIAN"];
  for (const ev of events)
    for (const s of slots(ev)) {
      lines.push(
        "BEGIN:VEVENT",
        `UID:${s.uid}@hackevents`,
        `DTSTAMP:${icsStamp(now.toISOString())}`,
        `DTSTART:${icsStamp(s.start)}`,
        `DTEND:${icsStamp(s.end)}`,
        `SUMMARY:${icsEscape(s.summary)}`,
      );
      if (s.location) lines.push(`LOCATION:${icsEscape(s.location)}`);
      lines.push("END:VEVENT");
    }
  lines.push("END:VCALENDAR");
  return lines.map(icsFold).join("\r\n") + "\r\n";
}

/** "Add to Google Calendar" link for the event's run, or its deadline when no dates are listed. */
export function googleCalendarUrl(ev: Item): string | null {
  const s = slots(ev).find((x) => x.uid.endsWith("-event")) ?? slots(ev)[0];
  if (!s) return null;
  const q = new URLSearchParams({
    action: "TEMPLATE",
    text: s.summary,
    dates: `${icsStamp(s.start)}/${icsStamp(s.end)}`,
  });
  if (s.location) q.set("location", s.location);
  return `https://calendar.google.com/calendar/render?${q}`;
}
