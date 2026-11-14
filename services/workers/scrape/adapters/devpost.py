"""Devpost adapter (HE-024): the public listing JSON behind devpost.com/hackathons.

Source URL: https://devpost.com/api/hackathons?status[]=upcoming&status[]=open&page=N
(robots.txt: `User-agent: * / Disallow:` = everything allowed for us).

The listing only has calendar dates ("Oct 30 - Dec 04, 2026"), no time and no timezone:
- registration_deadline stays None (the true deadline has a time + tz we do not have).
- event_start/event_end = 00:00 UTC on the stated calendar dates, timezone_stated=None,
  so the date (what dedupe keys on) is exact and downstream knows the time is unstated.
"""

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from workers.scrape.adapters.base import EventCandidate, Mode, ParseError

log = logging.getLogger(__name__)

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1
    )
}
_RANGE = re.compile(
    r"^(?P<m1>[A-Z][a-z]{2}) (?P<d1>\d{1,2})(?:, (?P<y1>\d{4}))?"
    r"(?: - (?:(?P<m2>[A-Z][a-z]{2}) )?(?P<d2>\d{1,2}), (?P<y2>\d{4})|, (?P<ys>\d{4}))$"
)


def _date(year: int, month: str, day: str) -> datetime:
    return datetime(year, _MONTHS[month], int(day), tzinfo=UTC)


def submission_period(text: Any) -> tuple[datetime | None, datetime | None]:
    """'Oct 30 - Dec 04, 2026' | 'Nov 11 - 30, 2026' | 'Jul 18, 2026 - Jan 14, 2027' |
    'Nov 14, 2026' -> (start, end) at 00:00 UTC. Anything else -> (None, None)."""
    m = _RANGE.match(text.strip()) if isinstance(text, str) else None
    if not m:
        return None, None
    try:
        if m["ys"]:
            day = _date(int(m["ys"]), m["m1"], m["d1"])
            return day, day
        m2 = m["m2"] or m["m1"]
        end = _date(int(m["y2"]), m2, m["d2"])
        y1 = int(m["y1"]) if m["y1"] else end.year - (_MONTHS[m["m1"]] > _MONTHS[m2])
        start = _date(y1, m["m1"], m["d1"])
    except (KeyError, ValueError):  # unknown month abbreviation, Feb 30, ...
        return None, None
    return (start, end) if start <= end else (None, None)


def _mode(loc: Any) -> tuple[Mode, str | None]:
    if not isinstance(loc, dict):
        return "unknown", None
    place = loc.get("location")
    if loc.get("icon") == "globe" or place == "Online":
        return "online", None
    if isinstance(place, str) and place.strip():
        return "offline", place
    return "unknown", None


def _prize(raw: Any) -> str | None:
    # "$<span data-currency-value>22,000</span>" -> "$22,000"; "$0" means no cash prize.
    if not isinstance(raw, str) or not re.search(r"[1-9]", raw):
        return None
    return raw


def parse_devpost(body: bytes, url: str) -> list[EventCandidate]:
    try:
        items = json.loads(body)["hackathons"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ParseError(f"not a Devpost listing: {exc}") from exc
    if not isinstance(items, list):
        raise ParseError("not a Devpost listing: 'hackathons' is not a list")
    out: list[EventCandidate] = []
    for h in items:
        if not isinstance(h, dict) or h.get("open_state") == "ended":
            continue
        start, end = submission_period(h.get("submission_period_dates"))
        mode, location = _mode(h.get("displayed_location"))
        try:
            out.append(
                EventCandidate(
                    title=h.get("title") or "",
                    organiser=h.get("organization_name") or "",
                    registration_url=h.get("url"),
                    registration_deadline=None,
                    event_start=start,
                    event_end=end,
                    mode=mode,
                    location=location,
                    eligibility=["Invite only"] if h.get("invite_only") else [],
                    prize=_prize(h.get("prize_amount")),
                    evidence=f"Submission period: {h.get('submission_period_dates') or ''}",
                    confidence=1.0,
                    extractor="devpost",
                )
            )
        except ValidationError as exc:
            log.warning("devpost item %s skipped: %s", h.get("id"), exc.errors()[0]["msg"])
    return out
