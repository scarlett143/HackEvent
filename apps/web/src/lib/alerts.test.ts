import { describe, expect, it } from "vitest";
import { buildAlerts, parseRead, serializeRead, unreadCount } from "./alerts";
import type { EventItem } from "./events";

const TZ = "Asia/Kolkata";
const NOW = new Date("2026-11-17T06:00:00Z"); // 11:30 IST on the 17th
const day = (n: number) => new Date(NOW.getTime() + n * 86_400_000).toISOString();
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
    is_tracked: false,
    ...over,
  }) as EventItem;

describe("buildAlerts", () => {
  const alerts = buildAlerts(
    [
      ev("far", { is_tracked: true, registration_deadline: day(10) }), // > 3 days: no alert
      ev("soon", { is_tracked: true, registration_deadline: day(2) }), // warning
      ev("today", { is_tracked: true, registration_deadline: day(0.5) }), // urgent
      ev("untracked", { registration_deadline: day(1) }), // not tracked: no alert
      ev("done", { is_tracked: true, is_registered: true, registration_deadline: day(1) }), // registered: no deadline nag
      ev("fresh", { is_new: true }),
      ev("closed", { is_tracked: true, registration_deadline: day(-1) }), // already closed
    ],
    NOW,
    TZ,
  );
  it("only alerts on tracked, unregistered, closing-soon deadlines", () => {
    expect(alerts.filter((a) => a.kind === "deadline").map((a) => a.event.id)).toEqual(["today", "soon"]);
  });
  it("orders urgent, then warning, then info, and keeps API order within a level", () => {
    expect(alerts.map((a) => `${a.level}:${a.kind}:${a.event.id}`)).toEqual([
      "urgent:deadline:today",
      "warning:deadline:soon",
      "info:registered:done", // before "fresh" in the input, so before it here
      "info:new:fresh",
    ]);
  });
  it("a deadline alert id changes when the urgency band changes, so it re-alerts", () => {
    const [urgent] = buildAlerts([ev("x", { is_tracked: true, registration_deadline: day(0.5) })], NOW, TZ);
    const [warning] = buildAlerts([ev("x", { is_tracked: true, registration_deadline: day(2) })], NOW, TZ);
    expect(urgent.id).not.toBe(warning.id);
  });
});

describe("read state", () => {
  it("parseRead tolerates garbage", () => {
    expect(parseRead(null).size).toBe(0);
    expect(parseRead("{not json").size).toBe(0);
    expect(parseRead('{"a":1}').size).toBe(0);
    expect([...parseRead('["a",1,"b"]')]).toEqual(["a", "b"]);
  });
  it("round-trips and caps the stored ids, keeping the newest", () => {
    const big = new Set(Array.from({ length: 600 }, (_, i) => `id${i}`));
    const back = parseRead(serializeRead(big));
    expect(back.size).toBe(500);
    expect(back.has("id599")).toBe(true);
    expect(back.has("id0")).toBe(false);
  });
  it("unreadCount ignores read ids", () => {
    const a = buildAlerts([ev("n1", { is_new: true }), ev("n2", { is_new: true })], NOW, TZ);
    expect(unreadCount(a, new Set(["new:n1"]))).toBe(1);
  });
});
