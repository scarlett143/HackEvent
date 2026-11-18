"""HE-027: JS-rendered fetch through the browser container.

The render test needs a Playwright server: `uv run playwright install chromium` then
`uv run playwright run-server --port 3000` and TEST_BROWSER_WS_URL=ws://127.0.0.1:3000/
(CI does this). Nothing here touches the network: the document and every request the
browser makes are answered from tests/fixtures/render via the intercept + guard.
"""

import hashlib
import ipaddress
import os
from pathlib import Path
from typing import Any

import httpx2
import pytest

from common.fetch import render as render_mod
from common.fetch.fetcher import FetchError, FetchResult
from common.fetch.render import allowed, render
from common.fetch.ssrf import BlockedURL, check_url

FIXTURES = Path(__file__).parent / "fixtures" / "render"
BROWSER_WS = os.environ.get("TEST_BROWSER_WS_URL", "")
needs_browser = pytest.mark.skipif(not BROWSER_WS, reason="TEST_BROWSER_WS_URL not set")
PAGE = "https://events.example.org/hackathons"


def _result(body: bytes, url: str, ctype: str, status: int = 200) -> FetchResult:
    return FetchResult(
        status=status,
        final_url=url,
        headers=httpx2.Headers({"content-type": ctype}),
        body=body,
        content_hash=hashlib.sha256(body).hexdigest(),
        not_modified=False,
    )


@pytest.mark.parametrize(
    ("rtype", "method", "main", "ok"),
    [
        ("document", "GET", True, True),
        ("document", "GET", False, False),  # iframes
        ("script", "GET", False, True),
        ("xhr", "GET", True, True),
        ("fetch", "GET", True, True),
        ("fetch", "POST", True, False),  # never send data anywhere
        ("image", "GET", True, False),
        ("stylesheet", "GET", True, False),
        ("font", "GET", True, False),
        ("media", "GET", True, False),
        ("websocket", "GET", True, False),
        ("other", "GET", True, False),
    ],
)
def test_request_policy(rtype: str, method: str, main: bool, ok: bool) -> None:
    assert allowed(rtype, method, main_frame=main) is ok


async def test_no_browser_configured_is_a_fetch_error() -> None:
    with pytest.raises(FetchError, match="BROWSER_WS_URL"):
        await render(PAGE, redis=None, ws_endpoint="")  # type: ignore[arg-type]


async def test_304_and_http_errors_skip_the_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    for first in (
        FetchResult(304, PAGE, httpx2.Headers(), b"", None, True),
        _result(b"gone", PAGE, "text/html", status=404),
    ):

        async def fake_fetch(*a: Any, _r: FetchResult = first, **kw: Any) -> FetchResult:
            return _r

        monkeypatch.setattr(render_mod, "fetch", fake_fetch)
        # An unreachable endpoint proves the browser is never contacted.
        assert await render(PAGE, redis=None, ws_endpoint="ws://127.0.0.1:9/") is first  # type: ignore[arg-type]


@needs_browser
async def test_js_page_fixture_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(host: str, port: int) -> list[Any]:
        return [ipaddress.ip_address("93.184.216.34")]  # public: no real DNS

    monkeypatch.setattr("common.fetch.ssrf.resolve", resolve)

    async def fake_fetch(url: str, *a: Any, **kw: Any) -> FetchResult:
        assert kw["respect_robots"] is True
        return _result((FIXTURES / "js_page.html").read_bytes(), url, "text/html; charset=utf-8")

    served = {
        "https://events.example.org/app.js": ("app.js", "text/javascript"),
        "https://events.example.org/api/events.json": ("events.json", "application/json"),
    }
    asked: list[str] = []
    blocked: list[str] = []

    async def guarded(url: str) -> FetchResult:
        asked.append(url)
        try:
            await check_url(url)  # the real SSRF guard decides first
        except BlockedURL:
            blocked.append(url)
            raise
        if url not in served:
            raise FetchError(f"unexpected {url}")
        name, ctype = served[url]
        return _result((FIXTURES / name).read_bytes(), url, ctype)

    monkeypatch.setattr(render_mod, "fetch", fake_fetch)
    out = await render(PAGE, redis=None, ws_endpoint=BROWSER_WS, guarded=guarded)  # type: ignore[arg-type]

    html = out.body.decode()
    assert "Example Spring Hackathon 2027" in html and "Loading…" not in html
    assert out.content_hash == hashlib.sha256(out.body).hexdigest()
    assert out.status == 200 and out.final_url == PAGE
    # Only the script and the GET XHRs reached the guard; CSS, image, iframe, POST never did.
    assert set(asked) == {*served, "http://169.254.169.254/latest/meta-data/"}
    assert blocked == ["http://169.254.169.254/latest/meta-data/"]
