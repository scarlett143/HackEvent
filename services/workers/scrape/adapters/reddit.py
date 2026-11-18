"""Reddit listing adapter (HE-028): `GET oauth.reddit.com/r/<sub>/new` JSON -> candidates.

Listing shape (Reddit API docs): {"kind": "Listing", "data": {"children": [{"kind": "t3",
"data": {title, selftext, url, permalink, subreddit, created_utc, is_self, over_18,
stickied, removed_by_category, ...}}]}}.

Like RSS, a post is an announcement, not a schedule: `created_utc` is posting time, so no
event dates or deadline are set (the LLM path can enrich from the linked page). Posts with
no doc 08 keyword, NSFW, stickied (rules / megathreads) or removed posts are skipped.
Author names are never stored.
"""

import json
import logging

from pydantic import ValidationError

from workers.keywords import has_keyword
from workers.scrape.adapters.base import EventCandidate, ParseError, to_text

log = logging.getLogger(__name__)

CONFIDENCE = 0.5  # a post is not known to be a hackathon until the page is read
SITE = "https://www.reddit.com"


def parse_reddit(body: bytes, url: str) -> list[EventCandidate]:
    try:
        listing = json.loads(body)
        children = listing["data"]["children"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ParseError(f"not a Reddit listing: {exc}") from exc
    if listing.get("kind") != "Listing" or not isinstance(children, list):
        raise ParseError("not a Reddit listing")
    out: list[EventCandidate] = []
    for child in children:
        post = child.get("data") if isinstance(child, dict) and child.get("kind") == "t3" else None
        if not isinstance(post, dict):
            continue
        if post.get("over_18") or post.get("stickied") or post.get("removed_by_category"):
            continue
        title = post.get("title") or ""
        text = post.get("selftext") or ""
        if not has_keyword(to_text(f"{title} {text}")):
            continue
        permalink = post.get("permalink")
        thread = (
            f"{SITE}{permalink}"
            if isinstance(permalink, str) and permalink.startswith("/")
            else None
        )
        link = None if post.get("is_self") else post.get("url")
        try:
            out.append(
                EventCandidate(
                    title=title,
                    organiser=f"r/{post.get('subreddit') or 'reddit'}",
                    registration_url=link or thread,
                    description=text or None,
                    evidence=title,
                    confidence=CONFIDENCE,
                    extractor="reddit",
                )
            )
        except ValidationError as exc:
            log.warning("reddit post %s skipped: %s", post.get("id"), exc.errors()[0]["msg"])
    return out
