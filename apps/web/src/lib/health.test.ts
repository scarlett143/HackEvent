import { describe, expect, it } from "vitest";
import { filterHealth } from "./health";

const rows = [
  { url: "https://devpost.com/api", organisation_name: "Devpost", kind: "aggregator", adapter: "api", status: "green" as const },
  { url: "https://mlh.io/events.json", organisation_name: "MLH", kind: "official", adapter: "api", status: "amber" as const },
  { url: "https://unstop.com/x", organisation_name: null, kind: "aggregator", adapter: "selector", status: "red" as const },
];

describe("filterHealth", () => {
  it("returns everything for all + empty query", () => expect(filterHealth(rows, "all", "  ")).toHaveLength(3));
  it("filters by status", () => expect(filterHealth(rows, "red", "").map((r) => r.url)).toEqual(["https://unstop.com/x"]));
  it("searches url, org, kind and adapter case-insensitively", () => {
    expect(filterHealth(rows, "all", "MLH")).toHaveLength(1);
    expect(filterHealth(rows, "all", "selector")).toHaveLength(1);
    expect(filterHealth(rows, "all", "aggregator")).toHaveLength(2);
  });
  it("combines status and query", () => expect(filterHealth(rows, "green", "mlh")).toEqual([]));
});
