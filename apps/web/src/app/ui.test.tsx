import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { onNextReturn, SourceBlock } from "./ui";

describe("SourceBlock", () => {
  it("renders the scraped snippet as plain text, never HTML", () => {
    const html = renderToStaticMarkup(
      <SourceBlock
        tz="Asia/Kolkata"
        source={{
          url: "https://example.com/hack",
          snippet: "<script>alert(1)</script><b>Registrations close 20 Nov</b>",
          found_at: "2026-11-13T04:00:00Z",
        }}
      />,
    );
    expect(html).not.toContain("<script>");
    expect(html).not.toContain("<b>");
    expect(html).toContain("&lt;script&gt;alert(1)&lt;/script&gt;&lt;b&gt;Registrations close 20 Nov");
    expect(html).toContain("Found on 13 Nov 2026 via");
    expect(html).toContain('href="https://example.com/hack"');
    expect(html).toContain('rel="noopener noreferrer"');
  });

  it("does not link non-http sources", () => {
    const html = renderToStaticMarkup(
      <SourceBlock
        tz="Asia/Kolkata"
        source={{ url: "javascript:alert(1)", snippet: null, found_at: "2026-11-13T04:00:00Z" }}
      />,
    );
    expect(html).not.toContain("href=");
    expect(html).toContain("unknown source");
  });
});

describe("onNextReturn", () => {
  function fakeDoc() {
    const target = new EventTarget();
    const doc = {
      visibilityState: "visible" as DocumentVisibilityState,
      addEventListener: target.addEventListener.bind(target),
      removeEventListener: target.removeEventListener.bind(target),
    };
    const set = (v: DocumentVisibilityState) => {
      doc.visibilityState = v;
      target.dispatchEvent(new Event("visibilitychange"));
    };
    return { doc, set };
  }

  it("fires once, when the tab comes back", () => {
    const { doc, set } = fakeDoc();
    let calls = 0;
    onNextReturn(doc, () => calls++);
    set("hidden"); // user switched to the organiser's tab
    expect(calls).toBe(0);
    set("visible");
    set("hidden");
    set("visible");
    expect(calls).toBe(1);
  });

  it("can be cancelled", () => {
    const { doc, set } = fakeDoc();
    let calls = 0;
    const cancel = onNextReturn(doc, () => calls++);
    cancel();
    set("hidden");
    set("visible");
    expect(calls).toBe(0);
  });
});
