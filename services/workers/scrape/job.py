"""`scrape_source` job (TRD §4.1 steps 2-3). DB-free: the container has no DATABASE_URL.

Hand-off: the scheduler puts everything this job needs in the payload; the job fetches and
enqueues `record_scrape(result)` on the scheduler queue, where the DB-holding scheduler
persists it. Nothing here touches Postgres (tests/test_scheduler.py checks the imports).
"""

from typing import Any

from arq import Retry

from common.config import get_settings
from common.fetch import FetchError, RetryLater, fetch
from common.fetch.reddit import fetch_listing
from common.fetch.render import render
from common.fetch.ssrf import BlockedURL
from common.queues import SCHEDULER_QUEUE

BODY_MAX = 200 * 1024  # doc 05: raw_documents.body truncated to 200 KB
MAX_TRIES = 5  # ScrapeWorker.max_tries; the last RetryLater is recorded as an error


def _body_text(body: bytes) -> str:
    # Postgres text rejects NUL; cut on bytes, drop a split trailing UTF-8 sequence.
    return body[:BODY_MAX].decode("utf-8", errors="ignore").replace("\x00", "")


async def scrape_source(ctx: dict[str, Any], source: dict[str, Any]) -> str:
    """Fetch one source. `source` keys: id, url, etag, last_modified, content_hash,
    respect_robots, render (optional: JS-render in the browser container, HE-027),
    reddit (optional: read via the Reddit API, HE-028).
    Returns the outcome ("changed" | "unchanged" | "error")."""
    result: dict[str, Any] = {"source_id": source["id"], "url": source["url"]}
    try:
        if source.get("reddit"):
            fetched = await fetch_listing(
                source["url"],
                source.get("etag"),
                source.get("last_modified"),
                redis=ctx["redis"],
            )
        elif source.get("render"):
            fetched = await render(
                source["url"],
                source.get("etag"),
                source.get("last_modified"),
                redis=ctx["redis"],
                ws_endpoint=get_settings().browser_ws_url,
                respect_robots=source["respect_robots"],
            )
        else:
            fetched = await fetch(
                source["url"],
                source.get("etag"),
                source.get("last_modified"),
                redis=ctx["redis"],
                respect_robots=source["respect_robots"],
            )
    except RetryLater as e:
        if ctx.get("job_try", 1) < MAX_TRIES:
            raise Retry(defer=e.retry_after) from None
        result |= {"outcome": "error", "error": f"retry limit reached: {e}"}
    except (BlockedURL, FetchError) as e:  # RobotsDisallowed is a FetchError
        result |= {"outcome": "error", "error": f"{type(e).__name__}: {e}"}
    else:
        if fetched.status >= 400:
            result |= {"outcome": "error", "error": f"HTTP {fetched.status}"}
        else:
            changed = not fetched.not_modified and fetched.content_hash != source["content_hash"]
            result |= {
                "outcome": "changed" if changed else "unchanged",
                "final_url": fetched.final_url,
                "etag": fetched.headers.get("etag"),
                "last_modified": fetched.headers.get("last-modified"),
                "content_hash": fetched.content_hash,
                "body": _body_text(fetched.body) if changed else None,
            }
    await ctx["redis"].enqueue_job("record_scrape", result, _queue_name=SCHEDULER_QUEUE)
    return str(result["outcome"])
