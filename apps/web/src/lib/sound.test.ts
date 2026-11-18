import { describe, expect, it } from "vitest";
import { parseSound, shouldChime } from "./sound";

describe("notification sound", () => {
  it("chimes only when the in-app count goes up after the first load", () => {
    expect(shouldChime(null, 3)).toBe(false); // first load
    expect(shouldChime(2, 3)).toBe(true);
    expect(shouldChime(3, 3)).toBe(false);
    expect(shouldChime(3, 0)).toBe(false); // marked read
  });
  it("is on by default and off only when turned off", () => {
    expect(parseSound(null)).toBe(true);
    expect(parseSound("on")).toBe(true);
    expect(parseSound("off")).toBe(false);
  });
});
