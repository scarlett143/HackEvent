"""JS-rendered fetch (HE-027): Chromium in the `browser` container, every byte via our guard.

`render(url, ...)`:
1. the document itself comes from the normal polite `fetch` (robots, per-host rate
   limit, ETag / If-Modified-Since). 304 or an HTTP error is returned as is: no render;
2. the remote browser (BROWSER_WS_URL, `playwright run-server`) loads it with *every*
   request intercepted (`context.route`). The browser never fetches anything itself:
   - the first main-frame document request is answered with the body from step 1;
   - script / xhr / fetch / later main-frame documents: GET only, each one through
     `fetch_guarded` (SSRF check per hop, IP pinning, size + time caps), at most
     MAX_SUBREQUESTS per page;
   - everything else (images, fonts, CSS, media, iframes, POSTs, ...) is aborted;
3. the rendered DOM (`page.content()`) is the body; content_hash is over it.

Defence in depth (Security doc §8, §7): the browser container sits on an `internal`
Docker network with no egress, so anything that slipped past `route` (WebSocket,
WebRTC) has nowhere to go. Downloads off, service workers blocked, no DB credentials.
"""

import asyncio
import hashlib
from collections.abc import Awaitable, Callable

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Request, Route, async_playwright
from redis.asyncio import Redis

from common.fetch.fetcher import FetchError, FetchResult, fetch, fetch_guarded, user_agent
from common.fetch.ssrf import BlockedURL

RENDER_TIMEOUT_S = 45.0
GOTO_TIMEOUT_MS = 30_000
IDLE_WAIT_MS = 5_000  # extra wait for late XHR after `load`; a page that never idles is fine
# ponytail: subresources skip the per-host rate limit (CDN scripts); the cap bounds them.
MAX_SUBREQUESTS = 40
FETCHED_TYPES = frozenset({"script", "xhr", "fetch"})

Guarded = Callable[[str], Awaitable[FetchResult]]


def allowed(request_type: str, method: str, *, main_frame: bool) -> bool:
    """Requests we fetch for the browser (via the guard); every other one is aborted."""
    if method != "GET":
        return False
    if request_type == "document":
        return main_frame
    return request_type in FETCHED_TYPES


def _headers(result: FetchResult) -> dict[str, str]:
    ctype = result.headers.get("content-type")
    return {"content-type": ctype} if ctype else {}


async def render(
    url: str,
    etag: str | None = None,
    last_modified: str | None = None,
    *,
    redis: Redis,
    ws_endpoint: str,
    respect_robots: bool = True,
    guarded: Guarded = fetch_guarded,
) -> FetchResult:
    """Raises BlockedURL, RobotsDisallowed, RetryLater, FetchError (like `fetch`)."""
    if not ws_endpoint:
        raise FetchError("render requested but BROWSER_WS_URL is not set")
    first = await fetch(url, etag, last_modified, redis=redis, respect_robots=respect_robots)
    if first.not_modified or first.status >= 400:
        return first
    try:
        async with asyncio.timeout(RENDER_TIMEOUT_S):
            html = await _render(first, ws_endpoint, guarded)
    except TimeoutError:
        raise FetchError(f"render timed out after {RENDER_TIMEOUT_S} s") from None
    except PlaywrightError as e:
        raise FetchError(f"render: {e.message.splitlines()[0] if e.message else e}") from None
    body = html.encode()
    return FetchResult(
        status=first.status,
        final_url=first.final_url,
        headers=first.headers,
        body=body,
        content_hash=hashlib.sha256(body).hexdigest(),
        not_modified=False,
    )


async def _render(first: FetchResult, ws_endpoint: str, guarded: Guarded) -> str:
    served_document = False
    budget = MAX_SUBREQUESTS

    async with async_playwright() as pw:
        browser = await pw.chromium.connect(ws_endpoint, timeout=10_000)
        try:
            context = await browser.new_context(
                accept_downloads=False, service_workers="block", user_agent=user_agent()
            )
            page = await context.new_page()

            async def handle(route: Route, request: Request) -> None:
                nonlocal served_document, budget
                main = request.frame == page.main_frame
                if not allowed(request.resource_type, request.method, main_frame=main):
                    await route.abort()
                    return
                if request.resource_type == "document" and not served_document:
                    served_document = True
                    await route.fulfill(
                        status=first.status, headers=_headers(first), body=first.body
                    )
                    return
                if budget <= 0:
                    await route.abort()
                    return
                budget -= 1
                try:
                    result = await guarded(request.url)
                except (BlockedURL, FetchError):
                    await route.abort()
                    return
                await route.fulfill(
                    status=result.status, headers=_headers(result), body=result.body
                )

            await context.route("**/*", handle)
            await page.goto(first.final_url, wait_until="load", timeout=GOTO_TIMEOUT_MS)
            try:
                await page.wait_for_load_state("networkidle", timeout=IDLE_WAIT_MS)
            except PlaywrightError:
                pass  # still busy: take the DOM as it is
            return await page.content()
        finally:
            await browser.close()
