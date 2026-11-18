"""HE-022: SSRF guard + guarded fetcher. No real network: patched resolver + MockTransport.

Robots/rate-limit tests need a throwaway Redis (TEST_REDIS_URL), e.g.
    docker run -d --rm --name he022-redis -p 56492:6379 redis:7-alpine
    TEST_REDIS_URL=redis://localhost:56492/0 uv run pytest services/tests/test_fetch.py
"""

import hashlib
import ipaddress
import os
from collections.abc import AsyncIterator, Callable

import httpx2
import pytest
from redis.asyncio import Redis

from common.config import get_settings
from common.fetch import (
    BlockedURL,
    FetchError,
    RetryLater,
    RobotsDisallowed,
    check_url,
    fetch,
    fetch_guarded,
    fetcher,
    ssrf,
)

PUBLIC = "93.184.215.14"
DNS: dict[str, list[str]] = {
    "example.com": [PUBLIC],
    "v6.example.com": ["2606:2800:21f:cb07:6820:80da:af6b:8b2c"],
    "mixed.example.com": [PUBLIC, "10.0.0.5"],
    "metadata.example.com": ["169.254.169.254"],
    "other.example.com": ["93.184.215.15"],
}
PRIVATE_BY_DNS = [
    "169.254.169.254",
    "10.1.2.3",
    "172.16.0.1",
    "172.31.255.255",
    "192.168.1.1",
    "127.0.0.1",
    "0.0.0.0",
    "100.64.0.1",
    "::1",
    "fc00::1",
    "fd00::1",
    "fe80::1",
    "::ffff:127.0.0.1",
    "::ffff:10.0.0.1",
]


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def resolve(host: str, port: int) -> list[ssrf.IPAddress]:
        calls.append(host)
        if host not in DNS:
            raise OSError("NXDOMAIN")
        return [ipaddress.ip_address(a) for a in DNS[host]]

    monkeypatch.setattr(ssrf, "resolve", resolve)
    return calls


Handler = Callable[[httpx2.Request], httpx2.Response]


def client_for(handler: Handler) -> tuple[httpx2.AsyncClient, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    return httpx2.AsyncClient(transport=httpx2.MockTransport(record)), seen


# --- check_url ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.8.9.10/",
        "http://10.0.0.1/",
        "http://172.16.0.1/",
        "http://172.31.255.254/",
        "http://192.168.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/",
        "http://100.64.0.1/",  # CGNAT
        "http://224.0.0.1/",  # multicast
        "http://240.0.0.1/",  # reserved
        "http://255.255.255.255/",
        "http://[::1]/",
        "http://[::]/",
        "http://[fc00::1]/",
        "http://[fdff:ffff::1]/",
        "http://[fe80::1]/",
        "http://[ff02::1]/",
        "http://[::ffff:127.0.0.1]/",  # IPv4-mapped
        "http://[::ffff:169.254.169.254]/",
        "http://[::127.0.0.1]/",  # IPv4-compatible
        "http://[2002:7f00:1::]/",  # 6to4 of 127.0.0.1
        "http://[2002:a9fe:a9fe::]/",  # 6to4 of 169.254.169.254
        "http://[64:ff9b::a00:1]/",  # NAT64 of 10.0.0.1
        "http://2130706433/",  # decimal 127.0.0.1
        "http://0x7f.1/",  # hex short form
        "http://0x7f000001/",
        "http://127.1/",
        "http://0177.0.0.1/",  # octal
        "http://2852039166/",  # decimal 169.254.169.254
        "http://localhost/",  # not in fake DNS -> unresolvable -> blocked
    ],
)
async def test_private_and_obfuscated_addresses_rejected(url: str) -> None:
    with pytest.raises(BlockedURL):
        await check_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://evil@127.0.0.1/",
        "http://example.com@127.0.0.1/",
        "http://user:pw@example.com/",  # userinfo rejected even for a public host
        "http://example.com\\@127.0.0.1/",
        "ftp://example.com/",
        "file:///etc/passwd",
        "gopher://example.com/",
        "javascript:alert(1)",
        "http://example.com:22/",
        "https://example.com:6379/",
        "http://exa mple.com/",
    ],
)
async def test_scheme_userinfo_port_tricks_rejected(url: str) -> None:
    with pytest.raises(BlockedURL):
        await check_url(url)


@pytest.mark.parametrize("address", PRIVATE_BY_DNS)
async def test_hostname_resolving_to_private_rejected(address: str) -> None:
    DNS["evil.example.com"] = [address]
    try:
        with pytest.raises(BlockedURL):
            await check_url("https://evil.example.com/")
    finally:
        del DNS["evil.example.com"]


async def test_one_public_one_private_record_rejected() -> None:
    with pytest.raises(BlockedURL, match="10.0.0.5"):
        await check_url("https://mixed.example.com/")


async def test_public_hosts_allowed() -> None:
    target = await check_url("https://example.com/events")
    assert str(target.ip) == PUBLIC
    assert str(target.pinned_url()) == f"https://{PUBLIC}/events"
    v6 = await check_url("http://v6.example.com/")
    assert v6.pinned_url().host == DNS["v6.example.com"][0]
    assert (await check_url("http://8.8.8.8/")).ip == ipaddress.ip_address("8.8.8.8")


async def test_explicit_port_allow_list() -> None:
    with pytest.raises(BlockedURL):
        await check_url("http://example.com:8080/")
    ok = await check_url("http://example.com:8080/", allowed_ports=frozenset({8080}))
    assert ok.url.port == 8080


# --- fetch_guarded -----------------------------------------------------------------------


async def test_connects_to_pinned_ip_with_host_header_and_sni(fake_dns: list[str]) -> None:
    client, seen = client_for(lambda r: httpx2.Response(200, content=b"hello"))
    result = await fetch_guarded("https://example.com/a?b=1", client=client)
    (request,) = seen
    assert request.url.host == PUBLIC  # connected by IP: no second DNS lookup to rebind
    assert request.headers["host"] == "example.com"
    assert request.extensions["sni_hostname"] == "example.com"  # cert checked for the name
    assert fake_dns == ["example.com"]
    ua = request.headers["user-agent"]
    assert ua.startswith("HackEventsBot/0.1 (+https://") and "Mozilla" not in ua
    assert result.final_url == "https://example.com/a?b=1"
    assert result.body == b"hello"
    assert result.content_hash == hashlib.sha256(b"hello").hexdigest()
    assert not result.not_modified


async def test_redirect_to_metadata_ip_rejected() -> None:
    client, seen = client_for(
        lambda r: httpx2.Response(302, headers={"location": "http://169.254.169.254/latest/"})
    )
    with pytest.raises(BlockedURL):
        await fetch_guarded("https://example.com/", client=client)
    assert len(seen) == 1  # the private hop was never requested


async def test_redirect_to_host_resolving_private_rejected() -> None:
    client, seen = client_for(
        lambda r: httpx2.Response(301, headers={"location": "https://metadata.example.com/x"})
    )
    with pytest.raises(BlockedURL):
        await fetch_guarded("https://example.com/", client=client)
    assert len(seen) == 1


async def test_redirects_followed_and_rechecked() -> None:
    def handler(r: httpx2.Request) -> httpx2.Response:
        if r.headers["host"] == "example.com":
            return httpx2.Response(302, headers={"location": "https://other.example.com/new"})
        return httpx2.Response(200, content=b"moved")

    client, seen = client_for(handler)
    result = await fetch_guarded("https://example.com/old", client=client)
    assert result.final_url == "https://other.example.com/new"
    assert [r.url.host for r in seen] == [PUBLIC, "93.184.215.15"]


async def test_too_many_redirects() -> None:
    client, seen = client_for(lambda r: httpx2.Response(302, headers={"location": "/again"}))
    with pytest.raises(FetchError, match="redirects"):
        await fetch_guarded("https://example.com/", client=client)
    assert len(seen) == fetcher.MAX_REDIRECTS + 1


async def test_body_over_cap_aborted_while_streaming() -> None:
    async def huge() -> AsyncIterator[bytes]:
        for _ in range(6):
            yield b"x" * (1024 * 1024)

    client, _ = client_for(lambda r: httpx2.Response(200, content=huge()))
    with pytest.raises(FetchError, match="body over"):
        await fetch_guarded("https://example.com/", client=client)


async def test_declared_content_length_over_cap_aborted() -> None:
    client, _ = client_for(
        lambda r: httpx2.Response(200, headers={"content-length": str(6 * 1024 * 1024)})
    )
    with pytest.raises(FetchError, match="content-length"):
        await fetch_guarded("https://example.com/", client=client)


async def test_conditional_get_304() -> None:
    def handler(r: httpx2.Request) -> httpx2.Response:
        if r.headers.get("if-none-match") == '"v1"':
            return httpx2.Response(304, headers={"etag": '"v1"'})
        return httpx2.Response(200, content=b"new", headers={"etag": '"v2"'})

    client, seen = client_for(handler)
    result = await fetch_guarded(
        "https://example.com/",
        etag='"v1"',
        last_modified="Wed, 21 Oct 2026 07:28:00 GMT",
        client=client,
    )
    assert result.not_modified and result.status == 304
    assert result.body == b"" and result.content_hash is None
    assert seen[0].headers["if-modified-since"] == "Wed, 21 Oct 2026 07:28:00 GMT"
    fresh = await fetch_guarded("https://example.com/", etag='"old"', client=client)
    assert not fresh.not_modified and fresh.headers["etag"] == '"v2"'


async def test_transport_error_wrapped() -> None:
    def boom(r: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    client, _ = client_for(boom)
    with pytest.raises(FetchError, match="ConnectError"):
        await fetch_guarded("https://example.com/", client=client)


# --- robots.txt + rate limit (real Redis) -------------------------------------------------

REDIS_URL = os.environ.get("TEST_REDIS_URL")
needs_redis = pytest.mark.skipif(not REDIS_URL, reason="TEST_REDIS_URL not set")


@pytest.fixture
async def redis(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Redis]:
    assert REDIS_URL
    monkeypatch.setattr(get_settings(), "fetch_domain_interval_s", 0.2)
    r = Redis.from_url(REDIS_URL)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


def site(robots: str | int) -> Handler:
    def handler(r: httpx2.Request) -> httpx2.Response:
        if r.url.path == "/robots.txt":
            if isinstance(robots, int):
                return httpx2.Response(robots)
            return httpx2.Response(200, text=robots)
        return httpx2.Response(200, content=b"page")

    return handler


ROBOTS = "User-agent: HackEventsBot\nDisallow: /private\n\nUser-agent: *\nDisallow: /\n"


@needs_redis
async def test_robots_disallow_honoured_and_cached(redis: Redis) -> None:
    client, seen = client_for(site(ROBOTS))
    with pytest.raises(RobotsDisallowed):
        await fetch("https://example.com/private/x", redis=redis, client=client)
    assert [r.url.path for r in seen] == ["/robots.txt"]  # page never requested
    result = await fetch("https://example.com/events", redis=redis, client=client)
    assert result.body == b"page"
    assert [r.url.path for r in seen] == ["/robots.txt", "/events"]  # robots from cache
    assert 0 < await redis.ttl("he:fetch:robots:https://example.com") <= fetcher.ROBOTS_TTL_S


@needs_redis
async def test_robots_skipped_for_social_sources(redis: Redis) -> None:
    client, seen = client_for(site(ROBOTS))
    await fetch("https://example.com/private/x", redis=redis, client=client, respect_robots=False)
    assert [r.url.path for r in seen] == ["/private/x"]


@needs_redis
async def test_robots_status_handling(redis: Redis) -> None:
    client, _ = client_for(site(404))
    assert (await fetch("https://example.com/x", redis=redis, client=client)).status == 200
    await redis.flushdb()
    client, _ = client_for(site(403))
    with pytest.raises(RobotsDisallowed):
        await fetch("https://example.com/x", redis=redis, client=client)
    await redis.flushdb()
    client, _ = client_for(site(503))
    with pytest.raises(RetryLater):
        await fetch("https://example.com/x", redis=redis, client=client)


@needs_redis
async def test_blocked_url_never_touches_robots_or_bucket(redis: Redis) -> None:
    client, seen = client_for(site(ROBOTS))
    with pytest.raises(BlockedURL):
        await fetch("http://169.254.169.254/", redis=redis, client=client)
    assert seen == [] and await redis.keys("he:fetch:*") == []


@needs_redis
async def test_token_bucket_limits_per_host(redis: Redis) -> None:
    await fetcher.acquire(redis, "example.com")
    with pytest.raises(RetryLater) as exc:
        await fetcher.acquire(redis, "example.com", max_wait=0)
    assert 0 < exc.value.retry_after <= 0.2
    await fetcher.acquire(redis, "other.example.com", max_wait=0)  # separate bucket
    await fetcher.acquire(redis, "example.com", max_wait=1)  # waits for the refill


@needs_redis
async def test_fetch_takes_a_token_per_request(
    redis: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fetcher, "RATE_MAX_WAIT_S", 0)
    monkeypatch.setattr(get_settings(), "fetch_domain_interval_s", 60)
    client, seen = client_for(site(404))
    await fetch("https://example.com/a", redis=redis, client=client, respect_robots=False)
    with pytest.raises(RetryLater):
        await fetch("https://example.com/b", redis=redis, client=client, respect_robots=False)
    assert [r.url.path for r in seen] == ["/a"]


async def test_extra_headers_never_follow_a_redirect_to_another_host() -> None:
    """HE-028: an API credential stays on the host it was meant for."""

    def handler(r: httpx2.Request) -> httpx2.Response:
        if r.headers["host"] == "example.com":
            return httpx2.Response(302, headers={"location": "https://other.example.com/new"})
        return httpx2.Response(200, content=b"moved")

    client, seen = client_for(handler)
    await fetch_guarded(
        "https://example.com/api",
        client=client,
        extra_headers={"Authorization": "Bearer s3cr3t-token"},
    )
    assert seen[0].headers["authorization"] == "Bearer s3cr3t-token"
    assert "authorization" not in seen[1].headers
