"""Reddit via the official API (HE-028, doc 06: "Official Reddit API (OAuth app)").

Anonymous reddit.com JSON / RSS is refused (403 / 404, checked 2026-11-18), so a source
`https://www.reddit.com/r/<sub>` is read as `https://oauth.reddit.com/r/<sub>/new` with an
app-only token (client_credentials grant, read-only, no user account):

- token: POST https://www.reddit.com/api/v1/access_token, HTTP Basic client id/secret,
  cached in Redis until shortly before `expires_in`. A 401 on the listing drops it;
- the listing GET goes through the normal guarded fetch (SSRF guard, rate limit, caps);
  the Authorization header never follows a redirect to another host (fetcher);
- robots.txt is not consulted: this is the API, governed by Reddit's API terms
  (doc 06 "[verify current access rules]"), not crawling.

Credentials: REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET (worker-scrape only). Unset = every
Reddit source fails with a clear error, nothing else is affected.
"""

import re

import httpx2
from redis.asyncio import Redis

from common.config import get_settings
from common.fetch.fetcher import TOTAL_TIMEOUT_S, FetchError, FetchResult, fetch

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API = "https://oauth.reddit.com"
TOKEN_KEY = "he:reddit:token"
LISTING_LIMIT = 50
_SUB = re.compile(r"^https://(?:www\.|old\.)?reddit\.com/r/([A-Za-z0-9_]{2,21})/?(?:new/?)?$")


def listing_url(source_url: str) -> str:
    """https://www.reddit.com/r/hackathons[/new] -> the API's newest-posts listing."""
    m = _SUB.match(source_url.strip())
    if not m:
        raise FetchError(f"not a subreddit URL: {source_url}")
    return f"{API}/r/{m.group(1)}/new?limit={LISTING_LIMIT}&raw_json=1"


def user_agent() -> str:
    # Reddit's required shape: <platform>:<app id>:<version> (by /u/<owner>)
    s = get_settings()
    return f"server:{s.reddit_client_id}:0.1 (HackEvents; +https://{s.app_domain}/bot)"


async def token(redis: Redis, *, client: httpx2.AsyncClient | None = None) -> str:
    cached = await redis.get(TOKEN_KEY)
    if cached:
        return cached.decode() if isinstance(cached, bytes) else str(cached)
    s = get_settings()
    secret = s.reddit_client_secret.get_secret_value()
    if not (s.reddit_client_id and secret):
        raise FetchError("Reddit API credentials are not configured (REDDIT_CLIENT_ID/SECRET)")
    http = client or httpx2.AsyncClient(trust_env=False, timeout=TOTAL_TIMEOUT_S)
    try:
        r = await http.post(
            TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(s.reddit_client_id, secret),
            headers={"User-Agent": user_agent()},
        )
    except httpx2.HTTPError as e:
        raise FetchError(f"Reddit token: {type(e).__name__}") from None
    finally:
        if client is None:
            await http.aclose()
    if r.status_code != 200:
        raise FetchError(f"Reddit token: HTTP {r.status_code}")
    try:
        body = r.json()
        value, ttl = str(body["access_token"]), int(body.get("expires_in", 3600))
    except (ValueError, KeyError, TypeError):
        raise FetchError("Reddit token: unexpected response") from None
    await redis.set(TOKEN_KEY, value, ex=max(60, ttl - 300))
    return value


async def fetch_listing(
    source_url: str,
    etag: str | None,
    last_modified: str | None,
    *,
    redis: Redis,
    client: httpx2.AsyncClient | None = None,
) -> FetchResult:
    """Newest posts of a subreddit as Reddit's listing JSON. Raises like `fetch`."""
    url = listing_url(source_url)
    bearer = await token(redis, client=client)
    result = await fetch(
        url,
        etag,
        last_modified,
        redis=redis,
        respect_robots=False,
        client=client,
        extra_headers={"Authorization": f"Bearer {bearer}", "User-Agent": user_agent()},
    )
    if result.status == 401:
        await redis.delete(TOKEN_KEY)  # expired / revoked: the next run gets a new one
    return result
