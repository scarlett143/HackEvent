import { describe, expect, it } from "vitest";
import {
  type EventItem,
  eventsQuery,
  filtersToSearch,
  formatDeadline,
  formatWhen,
  parseFilters,
  safeHttpUrl,
  sectionize,
  tMinus,
  urgency,
} from "./events";

const IST = "Asia/Kolkata";
// 15 Nov 2026, 10:00 IST
const NOW = new Date("2026-11-15T04:30:00Z");
// 23:59 IST on 15 + n Nov
const closesIn = (n: number) => new Date(Date.UTC(2026, 10, 15 + n, 18, 29)).toISOString();

describe("formatDeadline", () => {
  it("day 0 shows the closing time in the user's timezone", () => {
    expect(formatDeadline(closesIn(0), NOW, IST)).toBe("Closes today 23:59 IST");
  });

  it("counts local calendar days", () => {
    expect(formatDeadline(closesIn(1), NOW, IST)).toBe("1 day left to register");
    expect(formatDeadline(closesIn(2), NOW, IST)).toBe("2 days left to register");
  });

  it("converts to the given timezone, not the machine's", () => {
    // 18:29Z on 15 Nov is 03:29 on 16 Nov in Tokyo: tomorrow, not today.
    expect(formatDeadline(closesIn(0), NOW, "Asia/Tokyo")).toBe("1 day left to register");
    expect(formatWhen(closesIn(0), IST)).toBe("15 Nov, 23:59 IST");
    expect(formatWhen(closesIn(0), "UTC")).toBe("15 Nov, 18:29 UTC");
  });

  it("handles past and missing deadlines", () => {
    expect(formatDeadline("2026-11-15T04:00:00Z", NOW, IST)).toBe("Registration closed");
    expect(formatDeadline(null, NOW, IST)).toBe("No deadline listed");
  });
});

describe("urgency", () => {
  it.each([
    [0, "urgent"],
    [1, "urgent"],
    [2, "warning"],
    [3, "warning"],
    [4, "ok"],
    [30, "ok"],
  ])("%i days left → %s", (n, want) => {
    expect(urgency(closesIn(n), NOW, IST)).toBe(want);
  });

  it("closed and unknown", () => {
    expect(urgency("2026-11-14T00:00:00Z", NOW, IST)).toBe("closed");
    expect(urgency(null, NOW, IST)).toBe("none");
  });
});

describe("filters URL state", () => {
  it("round-trips through the query string", () => {
    const qs = "q=ai+hack&kind=company&tier=1&region=india&mode=online&status=open&within=7&registered=true&new_only=true";
    const f = parseFilters(new URLSearchParams(qs));
    expect(f).toEqual({
      q: "ai hack",
      kind: "company",
      tier: "1",
      region: "india",
      mode: "online",
      status: "open",
      within: "7",
      registered: true,
      new_only: true,
    });
    expect(parseFilters(new URLSearchParams(filtersToSearch(f)))).toEqual(f);
    expect(filtersToSearch(f)).toBe(qs);
  });

  it("drops unknown keys and values", () => {
    expect(parseFilters(new URLSearchParams("tier=9&mode=<x>&foo=1&registered=yes&q=%20"))).toEqual({});
  });

  it("resolves the deadline window for the API", () => {
    const sp = new URLSearchParams(eventsQuery({ within: "3", region: "global" }, NOW, "abc"));
    expect(sp.get("deadline_before")).toBe("2026-11-18T04:30:00.000Z");
    expect(sp.get("within")).toBeNull();
    expect(sp.get("region")).toBe("global");
    expect(sp.get("cursor")).toBe("abc");
    expect(sp.get("limit")).toBe("50");
  });
});

describe("safeHttpUrl", () => {
  it("allows only http(s)", () => {
    expect(safeHttpUrl("https://example.com/a")).toBe("https://example.com/a");
    expect(safeHttpUrl("javascript:alert(1)")).toBeNull();
    expect(safeHttpUrl("data:text/html,x")).toBeNull();
    expect(safeHttpUrl("not a url")).toBeNull();
    expect(safeHttpUrl(null)).toBeNull();
  });
});

describe("sectionize", () => {
  const ev = (id: string, deadline: string | null, is_new = false) =>
    ({ id, registration_deadline: deadline, is_new }) as EventItem;

  it("pins new, then closing this week, then later", () => {
    const s = sectionize(
      [ev("a", closesIn(1)), ev("b", closesIn(2), true), ev("c", closesIn(20)), ev("d", null)],
      NOW,
    );
    expect(s.fresh.map((e) => e.id)).toEqual(["b"]);
    expect(s.closing.map((e) => e.id)).toEqual(["a"]);
    expect(s.later.map((e) => e.id)).toEqual(["c", "d"]);
  });
});

describe("tMinus", () => {
  const now = new Date("2026-11-17T00:00:00Z");
  it("days + hours while more than a day away", () => {
    expect(tMinus("2026-11-19T04:30:00Z", now)).toBe("T-2d 04h");
  });
  it("hours + minutes inside the last day", () => {
    expect(tMinus("2026-11-17T05:12:00Z", now)).toBe("T-5h 12m");
  });
  it("null when passed, exactly now, or no deadline", () => {
    expect(tMinus("2026-11-16T23:59:00Z", now)).toBeNull();
    expect(tMinus("2026-11-17T00:00:00Z", now)).toBeNull();
    expect(tMinus(null, now)).toBeNull();
  });
});
