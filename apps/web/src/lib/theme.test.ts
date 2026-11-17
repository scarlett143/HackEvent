import { describe, expect, it } from "vitest";
import { THEME_INIT_SCRIPT, resolveTheme } from "./theme";

describe("resolveTheme", () => {
  it("a stored choice wins over the OS", () => {
    expect(resolveTheme("light", true)).toBe("light");
    expect(resolveTheme("dark", false)).toBe("dark");
  });
  it("falls back to the OS when nothing valid is stored", () => {
    expect(resolveTheme(null, true)).toBe("dark");
    expect(resolveTheme("banana", false)).toBe("light");
  });
  it("the inline script uses the same storage key and values", () => {
    expect(THEME_INIT_SCRIPT).toContain('"he-theme"');
    expect(THEME_INIT_SCRIPT).toContain("prefers-color-scheme: dark");
  });
});
