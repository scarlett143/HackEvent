import { describe, expect, it } from "vitest";
import { buildXls, stamp, xmlText } from "./xls";

const IST = "Asia/Kolkata";
const ev = (over: Record<string, unknown> = {}) => ({
  title: "Hack & Build",
  registration_deadline: "2026-11-19T18:29:00Z", // 23:59 IST
  event_start: "2026-11-21T04:30:00Z",
  event_end: null,
  location: "Mumbai",
  mode: "offline" as const,
  is_registered: true,
  is_tracked: false,
  registration_url: "https://example.com/r?a=1&b=2",
  ...over,
});

describe("xmlText", () => {
  it("escapes XML specials", () => expect(xmlText(`<a href="x">&'</a>`)).toBe("&lt;a href=&quot;x&quot;&gt;&amp;&apos;&lt;/a&gt;"));
  it("drops control characters XML 1.0 forbids but keeps tab, newline and CR", () => {
    expect(xmlText("a\u0000b\u0008c\u001fd\te\nf\rg")).toBe("abcd\te\nf\rg");
  });
});

describe("stamp", () => {
  it("formats in the given timezone as YYYY-MM-DD HH:mm (24 h)", () => {
    expect(stamp("2026-11-19T18:29:00Z", IST)).toBe("2026-11-19 23:59");
    expect(stamp("2026-11-19T18:29:00Z", "UTC")).toBe("2026-11-19 18:29");
    expect(stamp("2026-11-19T20:00:00Z", IST)).toBe("2026-11-20 01:30"); // next local day
  });
  it("is empty without a date", () => expect(stamp(null, IST)).toBe(""));
});

describe("buildXls", () => {
  const xml = buildXls([ev(), ev({ title: "Second", location: null, is_registered: false, is_tracked: true })], IST);
  it("is an Excel XML workbook with a header plus one row per event", () => {
    expect(xml.startsWith('<?xml version="1.0" encoding="UTF-8"?>')).toBe(true);
    expect(xml).toContain('<?mso-application progid="Excel.Sheet"?>');
    expect(xml.match(/<Row>/g)).toHaveLength(3);
    for (const h of ["Title", "Registration closes", "Registered", "Registration link"]) expect(xml).toContain(`>${h}<`);
  });
  it("shows times in the user's timezone and maps booleans and mode to words", () => {
    expect(xml).toContain(">2026-11-19 23:59<");
    expect(xml).toContain(">In person<");
    expect(xml).toContain(">Yes<");
    expect(xml).toContain(">No<");
  });
  it("escapes the title and the link", () => {
    expect(xml).toContain(">Hack &amp; Build<");
    expect(xml).toContain(">https://example.com/r?a=1&amp;b=2<");
  });
  it("every cell is an explicit String, so a formula-looking title is never evaluated", () => {
    const evil = buildXls([ev({ title: '=HYPERLINK("http://evil","x")' })], IST);
    expect(evil).toContain('ss:Type="String">=HYPERLINK(&quot;http://evil&quot;,&quot;x&quot;)<');
    expect(evil).not.toMatch(/<Cell[^>]*><Formula|ss:Formula/);
    expect(evil.match(/ss:Type="(?!String")[A-Za-z]+"/g)).toBeNull();
  });
  it("only real http(s) links are written", () => {
    expect(buildXls([ev({ registration_url: "javascript:alert(1)" })], IST)).not.toContain("javascript");
  });
  it("an empty list still yields a valid sheet with just the header", () => {
    expect(buildXls([], IST).match(/<Row>/g)).toHaveLength(1);
  });
});
