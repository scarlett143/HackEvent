"""Guarded HTTP fetcher for source scraping (TRD §4.1, Security doc §8).

`fetch` = per-host rate limit (Redis) + robots.txt (Redis-cached) + `fetch_guarded`.
`fetch_guarded` = SSRF check on every hop, IP-pinned connection, manual redirects (max 5),
5 MB body cap, 20 s total timeout, conditional GET. Honest User-Agent, no impersonation.
"""

import asyncio
import hashlib
import time
from dataclasses import dataclass
from urllib.robotparser import RobotFileParser

import httpx2
from redis.asyncio import Redis

from common.config import get_settings
from common.fetch.ssrf import ALLOWED_PORTS, BlockedURL, check_url

MAX_BODY = 5 * 1024 * 1024
TOTAL_TIMEOUT_S = 20.0
MAX_REDIRECTS = 5
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
BOT_NAME = "HackEventsBot"
ROBOTS_TTL_S = 24 * 3600
ROBOTS_MAX = 512 * 1024  # what Google parses; the rest is ignored
RATE_MAX_WAIT_S = 30.0


def user_agent() -> str:
    return f"{BOT_NAME}/0.1 (+https://{get_settings().app_domain}/bot)"


class FetchError(Exception):
    """The fetch failed (too large, too many redirects, timeout, transport error)."""


class RetryLater(FetchError):
    """Not now: rate limit token unavailable or robots.txt temporarily unreachable."""

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class RobotsDisallowed(FetchError):
    """robots.txt forbids this URL for our bot."""


@dataclass(frozen=True)
class FetchResult:
    status: int
    final_url: str
    headers: httpx2.Headers
    body: bytes
    content_hash: str | None  # sha256 hex of body; None when not_modified
    not_modified: bool


def _client() -> httpx2.AsyncClient:
    # trust_env=False: no proxy env vars, so the pinned IP is really what we connect to.
    return httpx2.AsyncClient(trust_env=False, follow_redirects=False, timeout=TOTAL_TIMEOUT_S)


async def _get_once(
    client: httpx2.AsyncClient, url: httpx2.URL, headers: dict[str, str], ports: frozenset[int]
) -> tuple[httpx2.Response, bytes]:
    target = await check_url(url, allowed_ports=ports)
    host_header = url.netloc.decode("ascii")
    extensions = {"sni_hostname": url.host} if url.scheme == "https" else {}
    async with client.stream(
        "GET",
        target.pinned_url(),
        headers={**headers, "Host": host_header},
        extensions=extensions,
    ) as response:
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BODY:
            raise FetchError(f"body over {MAX_BODY} bytes (content-length {declared})")
        body = bytearray()
        async for chunk in response.aiter_bytes():  # decoded: caps decompression bombs too
            body += chunk
            if len(body) > MAX_BODY:
                raise FetchError(f"body over {MAX_BODY} bytes")
    return response, bytes(body)


async def fetch_guarded(
    url: str,
    etag: str | None = None,
    last_modified: str | None = None,
    *,
    client: httpx2.AsyncClient | None = None,
    allowed_ports: frozenset[int] = ALLOWED_PORTS,
) -> FetchResult:
    """GET `url` under the SSRF guard. Raises BlockedURL / FetchError."""
    headers = {"User-Agent": user_agent(), "Accept": "*/*"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    own = client is None
    http = client or _client()
    try:
        current = httpx2.URL(url)
        async with asyncio.timeout(TOTAL_TIMEOUT_S):
            for _ in range(MAX_REDIRECTS + 1):
                response, body = await _get_once(http, current, headers, allowed_ports)
                location = response.headers.get("location")
                if response.status_code in REDIRECT_STATUSES and location:
                    current = current.join(location)  # checked at the top of the next hop
                    continue
                not_modified = response.status_code == 304
                return FetchResult(
                    status=response.status_code,
                    final_url=str(current),
                    headers=response.headers,
                    body=body,
                    content_hash=None if not_modified else hashlib.sha256(body).hexdigest(),
                    not_modified=not_modified,
                )
            raise FetchError(f"more than {MAX_REDIRECTS} redirects")
    except httpx2.InvalidURL as e:  # bad start URL or Location header
        raise BlockedURL(f"invalid URL: {e}") from None
    except TimeoutError:
        raise FetchError(f"timed out after {TOTAL_TIMEOUT_S} s") from None
    except httpx2.HTTPError as e:
        raise FetchError(f"{type(e).__name__}: {e}") from None
    finally:
        if own:
            await http.aclose()


def _host_key(url: httpx2.URL) -> str:
    # ponytail: keyed by exact hostname, not registrable domain (eTLD+1 needs a public
    # suffix list dependency); a.example.com and b.example.com get separate buckets.
    return url.host


async def acquire(redis: Redis, host: str, *, max_wait: float | None = None) -> None:
    """Take this host's token, shared across worker processes via Redis.

    Token bucket of capacity 1 refilled every `fetch_domain_interval_s`: SET NX PX is the
    atomic take, the key's TTL is the time until the next token. Waits up to `max_wait`,
    then raises RetryLater so the job is re-queued instead of hammering the host.
    """
    interval_ms = max(1, int(get_settings().fetch_domain_interval_s * 1000))
    key = f"he:fetch:rate:{host}"
    deadline = time.monotonic() + (RATE_MAX_WAIT_S if max_wait is None else max_wait)
    while True:
        if await redis.set(key, "1", nx=True, px=interval_ms):
            return
        wait_s = max(int(await redis.pttl(key)), 1) / 1000
        if time.monotonic() + wait_s > deadline:
            raise RetryLater(f"rate limit for {host}", retry_after=wait_s)
        await asyncio.sleep(wait_s)


async def robots_allowed(
    redis: Redis, url: httpx2.URL, *, client: httpx2.AsyncClient | None = None
) -> bool:
    """robots.txt verdict for `url` (RFC 9309 + stdlib parser), cached per origin 24 h."""
    origin = f"{url.scheme}://{url.netloc.decode('ascii')}"
    key = f"he:fetch:robots:{origin}"
    cached = await redis.get(key)
    if cached is None:
        await acquire(redis, _host_key(url))
        result = await fetch_guarded(f"{origin}/robots.txt", client=client)
        if result.status >= 500:
            raise RetryLater(f"robots.txt for {origin}: HTTP {result.status}", retry_after=3600)
        if result.status in (401, 403):
            text = "User-agent: *\nDisallow: /\n"  # stdlib's conservative reading
        elif result.status >= 400:
            text = ""  # no robots.txt: everything allowed
        else:
            text = result.body[:ROBOTS_MAX].decode("utf-8", errors="replace")
        await redis.set(key, text, ex=ROBOTS_TTL_S)
    else:
        text = cached.decode() if isinstance(cached, bytes) else str(cached)
    parser = RobotFileParser()
    parser.parse(text.splitlines())
    return parser.can_fetch(BOT_NAME, str(url))


async def fetch(
    url: str,
    etag: str | None = None,
    last_modified: str | None = None,
    *,
    redis: Redis,
    respect_robots: bool = True,
    client: httpx2.AsyncClient | None = None,
) -> FetchResult:
    """Polite guarded GET. `respect_robots=False` only for social sources (TRD §4.1).

    Raises BlockedURL, RobotsDisallowed, RetryLater, FetchError.
    """
    parsed = (await check_url(url)).url  # reject before touching robots or the bucket
    if respect_robots and not await robots_allowed(redis, parsed, client=client):
        raise RobotsDisallowed(f"robots.txt disallows {url}")
    await acquire(redis, _host_key(parsed))
    return await fetch_guarded(url, etag, last_modified, client=client)
