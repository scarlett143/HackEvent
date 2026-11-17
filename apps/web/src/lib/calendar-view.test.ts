import { describe, expect, it } from "vitest";
import type { EventItem } from "./events";
import { addDays, agenda, dayKeyOf, entriesFor, entriesOnDay, monthGrid, step, weekDays, weekStart } from "./calendar-view";

const IST = "Asia/Kolkata";
const ev = (id: string, over: Partial<EventItem> = {}): EventItem =>
  ({
    id,
    title: `Event ${id}`,
    organiser: { name: "O", id: null, kind: null, tier: null, region: null },
    registration_url: null,
    registration_deadline: null,
    event_start: null,
    event_end: null,
    mode: "online",
    location: null,
    eligibility: [],
    status: "open",
    review_status: "auto",
    is_new: false,
    is_registered: false,
    is_tracked: true,
    ...over,
  }) as EventItem;

describe("day keys and grids", () => {
  it("dayKeyOf uses the user's timezone, not UTC", () => {
    expect(dayKeyOf(new Date("2026-11-19T20:00:00Z"), IST)).toBe("2026-11-20"); // 01:30 IST next day
    expect(dayKeyOf(new Date("2026-11-19T20:00:00Z"), "UTC")).toBe("2026-11-19");
  });
  it("addDays crosses month and year ends", () => {
    expect(addDays("2026-11-30", 1)).toBe("2026-12-01");
    expect(addDays("2026-12-31", 1)).toBe("2027-01-01");
    expect(addDays("2026-03-01", -1)).toBe("2026-02-28");
  });
  it("weeks start on Sunday", () => {
    expect(weekStart("2026-11-17")).toBe("2026-11-15"); // Tue -> Sun
    expect(weekDays("2026-11-17")).toHaveLength(7);
  });
  it("monthGrid is 42 cells starting on the Sunday on/before the 1st (Nov 2026 starts on a Sunday)", () => {
    const g = monthGrid("2026-11-17");
    expect(g).toHaveLength(42);
    expect(g[0]).toBe("2026-11-01");
    expect(g[41]).toBe("2026-12-12");
  });
  it("step moves by the unit of the view", () => {
    expect(step("2026-11-17", "day", 1)).toBe("2026-11-18");
    expect(step("2026-11-17", "week", -1)).toBe("2026-11-10");
    expect(step("2026-12-20", "month", 1)).toBe("2027-01-01");
    expect(step("2026-01-20", "month", -1)).toBe("2025-12-01");
  });
});

describe("entries", () => {
  const events = [
    ev("a", { registration_deadline: "2026-11-19T18:29:00Z" }),
    ev("b", { event_start: "2026-11-21T04:30:00Z", event_end: "2026-11-23T12:00:00Z" }),
    ev("untracked", { is_tracked: false, registration_deadline: "2026-11-19T10:00:00Z" }),
    ev("reg", { is_tracked: false, is_registered: true, registration_deadline: "2026-11-18T10:00:00Z" }),
  ];
  const all = entriesFor(events);
  it("keeps only tracked or registered events, soonest first, deadlines as points in time", () => {
    expect(all.map((e) => e.key)).toEqual(["reg-deadline", "a-deadline", "b-event"]);
    const d = all.find((e) => e.key === "a-deadline")!;
    expect(d.start.getTime()).toBe(d.end.getTime());
  });
  it("a multi-day run shows on every day it covers", () => {
    for (const day of ["2026-11-21", "2026-11-22", "2026-11-23"]) expect(entriesOnDay(all, day, IST).map((e) => e.key)).toContain("b-event");
    expect(entriesOnDay(all, "2026-11-24", IST)).toEqual([]);
  });
  it("agenda groups by day and clamps in-progress runs to the start day", () => {
    const g = agenda(all, "2026-11-22", IST);
    expect(g).toEqual([["2026-11-22", [all[2]]]]);
    expect(agenda(all, "2026-11-01", IST).map(([k]) => k)).toEqual(["2026-11-18", "2026-11-19", "2026-11-21"]);
  });
});
