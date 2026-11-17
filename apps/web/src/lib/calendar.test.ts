import { describe, expect, it } from "vitest";
import { buildIcs, googleCalendarUrl, icsEscape, icsFold, icsStamp } from "./calendar";

const ev = {
  id: "e1",
  title: "Hack, Build; Ship\nNow",
  registration_deadline: "2026-11-19T18:29:00.000Z",
  event_start: "2026-11-21T04:30:00.000Z",
  event_end: "2026-11-23T12:00:00.000Z",
  location: "Global Online",
};
const NOW = new Date("2026-11-17T00:00:00Z");

describe("ics helpers", () => {
  it("stamps in compact UTC", () => expect(icsStamp("2026-11-19T18:29:00.000Z")).toBe("20261119T182900Z"));
  it("escapes TEXT specials", () => expect(icsEscape("a,b;c\\d\ne")).toBe("a\\,b\\;c\\\\d\\ne"));
  it("folds long lines at 75 octets with CRLF+space, never splitting a character", () => {
    const folded = icsFold("X:" + "é".repeat(80));
    for (const part of folded.split("\r\n")) expect(new TextEncoder().encode(part).length).toBeLessThanOrEqual(75);
    expect(folded.replace(/\r\n /g, "")).toBe("X:" + "é".repeat(80));
  });
});

describe("buildIcs", () => {
  const ics = buildIcs([ev], NOW);
  it("is a CRLF calendar with a deadline slot and an event slot", () => {
    expect(ics.startsWith("BEGIN:VCALENDAR\r\n")).toBe(true);
    expect(ics.endsWith("END:VCALENDAR\r\n")).toBe(true);
    expect(ics.match(/BEGIN:VEVENT/g)).toHaveLength(2);
    expect(ics).toContain("UID:e1-deadline@hackevents");
    expect(ics).toContain("DTSTART:20261119T175900Z"); // deadline minus 30 min
    expect(ics).toContain("DTEND:20261123T120000Z");
    expect(ics).toContain("LOCATION:Global Online");
  });
  it("escapes the title so scraped text cannot inject properties", () => {
    expect(ics).toContain("SUMMARY:Hack\\, Build\\; Ship\\nNow");
    expect(ics.split("\r\n").some((l) => l.startsWith("Now"))).toBe(false);
  });
  it("skips events with no dates and defaults a missing end to one hour", () => {
    expect(buildIcs([{ ...ev, registration_deadline: null, event_start: null }], NOW)).not.toContain("VEVENT");
    expect(buildIcs([{ ...ev, registration_deadline: null, event_end: null }], NOW)).toContain("DTEND:20261121T053000Z");
  });
});

describe("googleCalendarUrl", () => {
  it("prefers the event run and url-encodes the title", () => {
    const u = new URL(googleCalendarUrl(ev)!);
    expect(u.origin + u.pathname).toBe("https://calendar.google.com/calendar/render");
    expect(u.searchParams.get("text")).toBe(ev.title);
    expect(u.searchParams.get("dates")).toBe("20261121T043000Z/20261123T120000Z");
  });
  it("falls back to the deadline, and is null with no dates", () => {
    const d = new URL(googleCalendarUrl({ ...ev, event_start: null })!);
    expect(d.searchParams.get("text")).toContain("Registration closes");
    expect(googleCalendarUrl({ ...ev, event_start: null, registration_deadline: null })).toBeNull();
  });
});
