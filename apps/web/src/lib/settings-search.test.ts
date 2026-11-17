import { describe, expect, it } from "vitest";
import { matchSettings, SUGGESTIONS } from "./settings-search";

describe("matchSettings", () => {
  it("an empty or whitespace query shows every section", () => {
    expect(matchSettings("").size).toBe(7);
    expect(matchSettings("   ").size).toBe(7);
  });
  it("matches case-insensitively on title and keywords", () => {
    expect([...matchSettings("QUIET")]).toEqual(["quiet"]);
    expect(matchSettings("time zone").has("timezone")).toBe(true);
  });
  it("requires every word to match", () => {
    expect([...matchSettings("quiet mute")]).toEqual(["quiet"]);
    expect(matchSettings("quiet banana").size).toBe(0);
  });
  it("punctuation in the query is ignored, not treated as a pattern", () => {
    expect(matchSettings("(.*)").size).toBe(7); // no usable words
    expect(matchSettings("ics?").has("export")).toBe(true);
  });
  it("every quick suggestion finds at least one section", () => {
    for (const s of SUGGESTIONS) expect(matchSettings(s).size).toBeGreaterThan(0);
  });
});
