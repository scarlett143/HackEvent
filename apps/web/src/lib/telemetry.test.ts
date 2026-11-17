import { describe, expect, it } from "vitest";
import { pctUsed, quotaLevel, untilReset, usd } from "./telemetry";

describe("telemetry helpers", () => {
  it("pctUsed rounds, tolerates a zero cap and can exceed 100", () => {
    expect(pctUsed("0.85000", "2.00")).toBe(43);
    expect(pctUsed("1", "0")).toBe(0);
    expect(pctUsed("2.50", "2.00")).toBe(125);
  });
  it("quotaLevel thresholds", () => {
    expect([quotaLevel(79), quotaLevel(80), quotaLevel(99), quotaLevel(100)]).toEqual([
      "ok",
      "warning",
      "warning",
      "urgent",
    ]);
  });
  it("untilReset formats hours/minutes and clamps at now", () => {
    const now = new Date("2026-11-17T20:00:00Z");
    expect(untilReset("2026-11-18T00:00:00Z", now)).toBe("4h 0m");
    expect(untilReset("2026-11-17T20:12:59Z", now)).toBe("0h 12m");
    expect(untilReset("2026-11-17T19:00:00Z", now)).toBe("now");
  });
  it("usd formats decimal strings", () => expect(usd("0.85000")).toBe("$0.85"));
});
