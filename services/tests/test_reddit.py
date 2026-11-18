"""HE-028: Reddit through the official API (no network: MockTransport + fake Redis)."""

from typing import Any

import httpx2
import pytest

from common.config import get_settings
from common.fetch import reddit
from common.fetch.fetcher import FetchError, FetchResult


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.ttl: dict[str, int] = {}

    async def get(self, key: str) -> Any:
        return self.data.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None) -> None:
        self.data[key] = value.encode() if isinstance(value, str) else value
        if ex:
            self.ttl[key] = ex

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)


@pytest.fixture
def creds(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("REDDIT_CLIENT_ID", "example-client")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "example-secret")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("url", "api"),
    [
        ("https://www.reddit.com/r/hackathons", "/r/hackathons/new"),
        ("https://www.reddit.com/r/hackathons/", "/r/hackathons/new"),
        ("https://old.reddit.com/r/Hackathon_IN/new/", "/r/Hackathon_IN/new"),
    ],
)
def test_listing_url(url: str, api: str) -> None:
    assert reddit.listing_url(url) == f"https://oauth.reddit.com{api}?limit=50&raw_json=1"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.reddit.com/r/hackathons/comments/abc/x/",
        "https://evil.example/r/hackathons",
        "http://www.reddit.com/r/hackathons",
        "https://www.reddit.com/r/a",  # too short for a subreddit name
        "https://www.reddit.com/user/someone",
    ],
)
def test_listing_url_rejects_non_subreddits(url: str) -> None:
    with pytest.raises(FetchError):
        reddit.listing_url(url)


async def test_token_without_credentials_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    get_settings.cache_clear()
    with pytest.raises(FetchError, match="not configured"):
        await reddit.token(FakeRedis())  # type: ignore[arg-type]
    get_settings.cache_clear()


async def test_token_is_fetched_once_and_cached(creds: Any) -> None:
    calls: list[httpx2.Request] = []

    def handler(r: httpx2.Request) -> httpx2.Response:
        calls.append(r)
        return httpx2.Response(200, json={"access_token": "tok-1", "expires_in": 86400})

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    redis = FakeRedis()
    assert await reddit.token(redis, client=client) == "tok-1"  # type: ignore[arg-type]
    assert await reddit.token(redis, client=client) == "tok-1"  # type: ignore[arg-type]
    assert len(calls) == 1
    req = calls[0]
    assert str(req.url) == reddit.TOKEN_URL and req.method == "POST"
    assert req.headers["authorization"].startswith("Basic ")
    assert b"grant_type=client_credentials" in req.content
    assert req.headers["user-agent"].startswith("server:example-client:")
    assert redis.ttl[reddit.TOKEN_KEY] == 86400 - 300  # refreshed before Reddit expires it


@pytest.mark.parametrize(
    "resp",
    [httpx2.Response(401, json={"error": 401}), httpx2.Response(200, json={"nope": 1})],
)
async def test_token_failures_are_fetch_errors(creds: Any, resp: httpx2.Response) -> None:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(lambda r: resp))
    with pytest.raises(FetchError, match="Reddit token"):
        await reddit.token(FakeRedis(), client=client)  # type: ignore[arg-type]


async def test_listing_sends_bearer_to_the_api_and_drops_token_on_401(
    creds: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []
    status = 200

    async def fake_fetch(url: str, etag: Any, lm: Any, **kw: Any) -> FetchResult:
        seen.append({"url": url, **kw})
        return FetchResult(status, url, httpx2.Headers(), b"{}", "h", False)

    monkeypatch.setattr(reddit, "fetch", fake_fetch)
    redis = FakeRedis()
    await redis.set(reddit.TOKEN_KEY, "tok-1")
    await reddit.fetch_listing("https://www.reddit.com/r/hackathons", None, None, redis=redis)  # type: ignore[arg-type]
    (call,) = seen
    assert call["url"] == "https://oauth.reddit.com/r/hackathons/new?limit=50&raw_json=1"
    assert call["extra_headers"]["Authorization"] == "Bearer tok-1"
    assert call["respect_robots"] is False  # API terms, not crawling
    assert reddit.TOKEN_KEY in redis.data

    status = 401
    await reddit.fetch_listing("https://www.reddit.com/r/hackathons", None, None, redis=redis)  # type: ignore[arg-type]
    assert reddit.TOKEN_KEY not in redis.data  # next run asks for a fresh token
