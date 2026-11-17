import { describe, expect, it } from "vitest";
import { filterHealth } from "./health";

const rows = [
  { url: "https://devpost.com/api", kind: "aggregator", adapter: "api", status: "green" as const },
  { url: "https://mlh.io/events.json", kind: "official", adapter: "api", status: "amber" as const },
  { url: "https://unstop.com/x", kind: "aggregator", adapter: "selector", status: "red" as const },
];

describe("filterHealth", () => {
  it("returns everything for all + empty query", () => expect(filterHealth(rows, "all", "  ")).toHaveLength(3));
  it("filters by status", () => expect(filterHealth(rows, "red", "").map((r) => r.url)).toEqual(["https://unstop.com/x"]));
  it("searches url, kind and adapter case-insensitively", () => {
    expect(filterHealth(rows, "all", "MLH")).toHaveLength(1);
    expect(filterHealth(rows, "all", "selector")).toHaveLength(1);
    expect(filterHealth(rows, "all", "aggregator")).toHaveLength(2);
  });
  it("does not search organisation names (organisations are hidden in the UI)", () => {
    const hidden = [{ ...rows[0], organisation_name: "Zzz Corp" }];
    expect(filterHealth(hidden, "all", "zzz")).toEqual([]);
  });
  it("combines status and query", () => expect(filterHealth(rows, "green", "mlh")).toEqual([]));
});
