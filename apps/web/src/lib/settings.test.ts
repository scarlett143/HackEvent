import { describe, expect, it } from "vitest";
import {
  formatQuietHours,
  offsetChips,
  offsetLabel,
  parseOffset,
  timezoneOptions,
  toggleOffset,
} from "./settings";

describe("parseOffset", () => {
  it.each([
    ["5", 5],
    [" 14d ", 14],
    ["10 days", 10],
    ["1 Day", 1],
    ["0", 0],
    ["30", 30],
  ])("%j → %j", (input, want) => expect(parseOffset(input)).toBe(want));

  it.each(["", "31", "-1", "2.5", "abc", "5 weeks", "100"])("rejects %j", (input) =>
    expect(parseOffset(input)).toBeNull(),
  );
});

describe("offsets", () => {
  it("toggles, dedupes and sorts descending", () => {
    expect(toggleOffset([3, 1], 7)).toEqual([7, 3, 1]);
    expect(toggleOffset([7, 3, 1], 3)).toEqual([7, 1]);
    expect(offsetChips([14, 3])).toEqual([14, 7, 3, 2, 1, 0]);
  });

  it("labels days", () => {
    expect([0, 1, 5].map(offsetLabel)).toEqual(["Day of", "1 day", "5 days"]);
  });
});

describe("formatQuietHours", () => {
  it("formats off, same-day and overnight ranges", () => {
    expect(formatQuietHours(null, null)).toBe("Off");
    expect(formatQuietHours("13:00", "14:00")).toBe("13:00–14:00");
    expect(formatQuietHours("22:00", "07:00")).toBe("22:00–07:00 (overnight)");
  });
});

describe("timezoneOptions", () => {
  it("always includes the current zone and the default, sorted and unique", () => {
    expect(timezoneOptions("Europe/Berlin", ["UTC", "Asia/Calcutta"])).toEqual([
      "Asia/Calcutta",
      "Asia/Kolkata",
      "Europe/Berlin",
      "UTC",
    ]);
  });
});
