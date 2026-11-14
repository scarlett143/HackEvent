"""Unstop adapter (HE-025): the public search JSON behind unstop.com/hackathons.

Source URL: https://unstop.com/api/public/opportunity/search-result
            ?opportunity=hackathons&page=N&per_page=10&oppstatus=open
robots.txt allows `/api/public/*` (only other `/api/*` paths are disallowed).

Dates carry explicit offsets ("2026-12-20T00:00:00+05:30") and are converted to UTC; a
value without an offset is ambiguous and left None. The listing has a registration
deadline (`regnRequirements.end_regn_dt`) and an end date, but no event start date.
"""

import json
import logging
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

from pydantic import ValidationError

from workers.scrape.adapters.base import EventCandidate, Mode, ParseError, parse_offset_datetime

log = logging.getLogger(__name__)

SITE = "https://unstop.com/"
_MODES: dict[Any, Mode] = {"online": "online", "offline": "offline", "hybrid": "hybrid"}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _location(item: dict[str, Any]) -> str | None:
    addr = _dict(item.get("address_with_country_logo"))
    parts = [addr.get("city"), addr.get("state"), _dict(addr.get("country")).get("name")]
    return ", ".join(p.strip() for p in parts if isinstance(p, str) and p.strip()) or None


def _prize(prizes: Any) -> str | None:
    if not isinstance(prizes, list):
        return None
    cash = [p for p in prizes if isinstance(p, dict) and isinstance(p.get("cash"), int | float)]
    currencies = {p.get("currencyCode") for p in cash}
    if not cash or len(currencies) != 1:
        return None
    total = sum(p["cash"] for p in cash)
    return f"{currencies.pop() or ''} {total:,} total".strip() if total > 0 else None


def _offset_name(raw: Any) -> str | None:
    try:
        return datetime.fromisoformat(raw).tzname() if isinstance(raw, str) else None
    except ValueError:
        return None


def parse_unstop(body: bytes, url: str) -> list[EventCandidate]:
    try:
        items = json.loads(body)["data"]["data"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ParseError(f"not an Unstop listing: {exc}") from exc
    if not isinstance(items, list):
        raise ParseError("not an Unstop listing: data.data is not a list")
    out: list[EventCandidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        regn = _dict(item.get("regnRequirements"))
        raw_deadline = regn.get("end_regn_dt")
        deadline = parse_offset_datetime(raw_deadline)
        link = item.get("seo_url") or (
            urljoin(SITE, item["public_url"]) if isinstance(item.get("public_url"), str) else None
        )
        mode = _MODES.get(item.get("region"), "unknown")
        filters = item.get("filters")
        eligible = [
            f["name"]
            for f in (filters if isinstance(filters, list) else [])
            if isinstance(f, dict)
            and f.get("type") == "eligible"
            and isinstance(f.get("name"), str)
        ]
        try:
            out.append(
                EventCandidate(
                    title=item.get("title") or "",
                    organiser=_dict(item.get("organisation")).get("name") or "",
                    registration_url=link,
                    registration_deadline=deadline,
                    event_start=None,
                    event_end=parse_offset_datetime(item.get("end_date")),
                    timezone_stated=_offset_name(raw_deadline) if deadline else None,
                    mode=mode,
                    location=_location(item) if mode != "online" else None,
                    eligibility=eligible,
                    prize=_prize(item.get("prizes")),
                    description=item.get("details"),
                    evidence=f"Registration deadline: {raw_deadline or ''}",
                    confidence=1.0,
                    extractor="unstop",
                )
            )
        except ValidationError as exc:
            log.warning("unstop item %s skipped: %s", item.get("id"), exc.errors()[0]["msg"])
    return out
