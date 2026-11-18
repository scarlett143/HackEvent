"""MLH adapter (HE-026): the season events page, https://www.mlh.com/seasons/<year>/events.

The page is server-rendered Inertia.js: the whole event list is JSON in
`<script data-page="app" type="application/json">` (at ~15-60 KB, inside the 200 KB
raw_documents cap). robots.txt allows `/seasons/`.

`upcomingEvents` carry exact UTC start/end ("2026-11-20T23:00:00Z"); `pastEvents` and any
item whose status is "ended" are skipped. MLH lists no registration deadline.
"""

import json
import logging
import re
from typing import Any
from urllib.parse import urljoin

from pydantic import ValidationError

from workers.scrape.adapters.base import EventCandidate, Mode, ParseError, parse_offset_datetime

log = logging.getLogger(__name__)

ORGANISER = "Major League Hacking (MLH)"  # organizationName is usually null
_PAGE = re.compile(rb'<script[^>]*\bdata-page="app"[^>]*>(.*?)</script>', re.DOTALL)


def _mode(format_type: Any) -> Mode:
    # Seen: "physical", "hybrid_physical"; MLH also runs digital events.
    if not isinstance(format_type, str):
        return "unknown"
    if format_type.startswith("hybrid"):
        return "hybrid"
    if format_type == "physical":
        return "offline"
    if format_type in ("digital", "virtual", "online"):
        return "online"
    return "unknown"


def parse_mlh(body: bytes, url: str) -> list[EventCandidate]:
    m = _PAGE.search(body)
    if not m:
        raise ParseError("not an MLH season page: no Inertia data-page script")
    try:
        items = json.loads(m.group(1))["props"]["upcomingEvents"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ParseError(f"not an MLH season page: {exc}") from exc
    if not isinstance(items, list):
        raise ParseError("not an MLH season page: 'upcomingEvents' is not a list")
    out: list[EventCandidate] = []
    for e in items:
        if not isinstance(e, dict) or e.get("status") == "ended":
            continue
        mode = _mode(e.get("formatType"))
        page = urljoin(url, e["url"]) if isinstance(e.get("url"), str) else None
        try:
            out.append(
                EventCandidate(
                    title=e.get("name") or "",
                    organiser=e.get("organizationName") or ORGANISER,
                    registration_url=e.get("websiteUrl") or page,
                    event_start=parse_offset_datetime(e.get("startsAt")),
                    event_end=parse_offset_datetime(e.get("endsAt")),
                    mode=mode,
                    location=e.get("location") if mode != "online" else None,
                    evidence=f"{e.get('dateRange') or ''} · {e.get('formatLabel') or ''}",
                    confidence=1.0,
                    extractor="mlh",
                )
            )
        except ValidationError as exc:
            log.warning("mlh item %s skipped: %s", e.get("id"), exc.errors()[0]["msg"])
    return out
