"""Guarded outbound HTTP for scrapers (HE-022)."""

from common.fetch.fetcher import (
    FetchError,
    FetchResult,
    RetryLater,
    RobotsDisallowed,
    fetch,
    fetch_guarded,
)
from common.fetch.ssrf import BlockedURL, check_url

__all__ = [
    "BlockedURL",
    "FetchError",
    "FetchResult",
    "RetryLater",
    "RobotsDisallowed",
    "check_url",
    "fetch",
    "fetch_guarded",
]
