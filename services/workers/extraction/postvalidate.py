"""Doc 08 A5 post-validation: code, not the LLM, decides what is trusted.

Input is the model's dict (already schema-checked by `parse_output`, but every field is
re-checked here so this is safe on any dict). Output is an `ExtractionResult` whose events
carry UTC datetimes, `status` and `needs_review` + reason codes.

Rules (doc 08 A5 unless marked *stricter*):
- strings trimmed and length-capped; `mode` outside the enum -> "unknown"; confidence not a
  number or outside 0..1 -> clamped + needs_review; events without title/organiser dropped;
  `is_hackathon=false` -> no events.
- `evidence` not a substring of the page text (whitespace-normalised) -> needs_review.
- dates must parse as ISO 8601 and be plausible (year within fetch year -1 .. +5), else
  the field is dropped + needs_review.
- *stricter*: the deadline's day must appear in `evidence` (A3: evidence is "the quote
  containing the dates"); A5 also accepts page text. Event dates must appear in evidence or
  page text. Unsupported date -> dropped + needs_review (never keep an invented date).
- naive datetimes: `timezone_stated` if resolvable, else the default (IST for Indian
  organisers, A4.5), else UTC + needs_review. Date-only deadline/end = 23:59:59 local,
  date-only start = 00:00 local.
- deadline in the past -> status=closed; deadline > 18 months out -> needs_review;
  event_start earlier than the deadline by > 1 day -> needs_review; end before start ->
  end dropped + needs_review.
- registration_url must be http(s) and on the source domain or a visible link on the page
  (*stricter*: plain-text URLs do not count, so "please output https://evil" text cannot
  smuggle a link), else dropped.
- *stricter*: instruction-like text quoted in evidence/free-text fields (an echo of a
  prompt-injection attempt) -> dates and URL dropped + needs_review.
Confidence < 0.7 / missing deadline -> Haiku verification is HE-045 (reads `confidence`).
"""

import math
import re
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from typing import Any, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from workers.extraction.prompt import PageText
from workers.extraction.schema import (
    ELIGIBILITY_ITEM_MAX,
    ELIGIBILITY_MAX_ITEMS,
    EVENTS_MAX,
    EVIDENCE_MAX,
    LOCATION_MAX,
    MODES,
    ORGANISER_MAX,
    PRIZE_MAX,
    TITLE_MAX,
    ExtractedCandidate,
    ExtractionResult,
)
from workers.scrape.adapters.base import Mode

MAX_DEADLINE_AHEAD = timedelta(days=548)  # 18 months
START_BEFORE_DEADLINE_SLACK = timedelta(days=1)
URL_MAX = 2_000

_TZ_ABBR = {
    "IST": "Asia/Kolkata",
    "UTC": "UTC",
    "GMT": "UTC",
    "Z": "UTC",
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "PT": "America/Los_Angeles",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "ET": "America/New_York",
    "CET": "Europe/Berlin",
    "CEST": "Europe/Berlin",
    "BST": "Europe/London",
    "SGT": "Asia/Singapore",
    "JST": "Asia/Tokyo",
    "AEST": "Australia/Sydney",
    "AEDT": "Australia/Sydney",
}
_UTC_OFFSET = re.compile(r"^(?:UTC|GMT)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)
_INJECTION = re.compile(
    r"ignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier)\s+"
    r"(?:instructions?|prompts?|rules?)|disregard\s+(?:all\s+|the\s+)?(?:previous|prior|above)"
    r"|system\s+prompt|you\s+are\s+now|new\s+instructions|developer\s+mode"
    r"|reveal\s+(?:your|the)\s+(?:instructions|prompt|rules)",
    re.IGNORECASE,
)

_MONTH_NAMES = (
    "jan january", "feb february", "mar march", "apr april", "may", "jun june",
    "jul july", "aug august", "sep sept september", "oct october", "nov november",
    "dec december",
)  # fmt: skip
_MONTHS = {name: i + 1 for i, names in enumerate(_MONTH_NAMES) for name in names.split()}
_MON = (
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
)
_ORD = r"(?!\d)(?:st|nd|rd|th)?"  # (?!\d): "January 2027" is not "Jan 20"
_RANGE = r"(?:\s*(?:-|–|—|to|&|and)\s*(\d{1,2})" + _ORD + r")?"
_DATE_PATTERNS = (
    # 6 February 2027, 6th Feb, 6-7 Feb 2027, 6 to 7 February
    re.compile(r"\b(\d{1,2})" + _ORD + _RANGE + r"\s+(?:of\s+)?" + _MON + r",?(?:\s+(\d{4}))?", re.IGNORECASE),
    # February 6, 2027, Feb 6-7 2027
    re.compile(_MON + r"\s+(\d{1,2})" + _ORD + _RANGE + r",?(?:\s+(\d{4}))?", re.IGNORECASE),
)  # fmt: skip
_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})")
_NUMERIC = re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4}|\d{2})\b")

DateMention = tuple[int | None, int, int]  # (year or None, month, day)


def date_mentions(text: str) -> set[DateMention]:
    """Every calendar day written in `text`, in the formats hackathon pages use."""
    found: set[DateMention] = set()
    for m in _ISO.finditer(text):
        found.add((int(m[1]), int(m[2]), int(m[3])))
    for m in _NUMERIC.finditer(text):
        y = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
        found |= {(y, int(m[2]), int(m[1])), (y, int(m[1]), int(m[2]))}  # dd/mm and mm/dd
    day_first, month_first = _DATE_PATTERNS
    for m in day_first.finditer(text):
        month, year = _MONTHS[m[3].lower().rstrip(".")], int(m[4]) if m[4] else None
        found |= {(year, month, int(d)) for d in (m[1], m[2]) if d}
    for m in month_first.finditer(text):
        month, year = _MONTHS[m[1].lower().rstrip(".")], int(m[4]) if m[4] else None
        found |= {(year, month, int(d)) for d in (m[2], m[3]) if d}
    return found


def _mentioned(day: date, mentions: set[DateMention]) -> bool:
    return any(m == day.month and d == day.day and y in (None, day.year) for y, m, d in mentions)


def _norm_ws(s: str) -> str:
    return " ".join(s.split())


def _str(value: Any, cap: int) -> str | None:
    if not isinstance(value, str):
        return None
    s = value.strip()
    return s[:cap] if s else None


def resolve_tz(name: str | None) -> tzinfo | None:
    if not name or not isinstance(name, str):
        return None
    name = name.strip()
    if name.upper() in _TZ_ABBR:
        return ZoneInfo(_TZ_ABBR[name.upper()])
    m = _UTC_OFFSET.match(name)
    if m:
        delta = timedelta(hours=int(m[2]), minutes=int(m[3] or 0))
        return timezone(delta if m[1] == "+" else -delta)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _parse(value: Any, tz: tzinfo, end_of_day: bool) -> tuple[datetime, date, bool] | None:
    """ISO 8601 -> (aware UTC datetime, calendar day as written, was it naive?).

    Naive values are taken in `tz`; a date-only deadline/end means 23:59:59 local."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            day = date.fromisoformat(s)
            dt = datetime.combine(day, time(23, 59, 59) if end_of_day else time())
        else:
            dt = datetime.fromisoformat(s)
            day = dt.date()
    except ValueError:
        return None
    naive = dt.tzinfo is None
    if naive:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(UTC), day, naive


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _url_ok(url: str, source_url: str, page: PageText) -> bool:
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return False
    if url in page.links:
        return True
    host, src = _host(url), _host(source_url)
    return bool(src) and (host == src or host.endswith("." + src) or src.endswith("." + host))


def _event(
    raw: dict[str, Any],
    *,
    page: PageText,
    page_norm: str,
    page_dates: set[DateMention],
    source_url: str,
    now: datetime,
    default_tz: str | None,
    extractor: str,
) -> ExtractedCandidate | None:
    title, organiser = _str(raw.get("title"), TITLE_MAX), _str(raw.get("organiser"), ORGANISER_MAX)
    if not title or not organiser:
        return None
    reasons: list[str] = []
    evidence = _str(raw.get("evidence"), EVIDENCE_MAX) or ""
    if not evidence or _norm_ws(evidence) not in page_norm:
        reasons.append("evidence_not_in_page")
    location, prize = _str(raw.get("location"), LOCATION_MAX), _str(raw.get("prize"), PRIZE_MAX)

    conf = raw.get("confidence")
    if isinstance(conf, bool) or not isinstance(conf, int | float) or math.isnan(conf):
        conf, reasons = 0.0, [*reasons, "confidence_invalid"]
    elif not 0 <= conf <= 1:
        conf, reasons = min(max(float(conf), 0.0), 1.0), [*reasons, "confidence_out_of_range"]

    mode = cast(Mode, raw["mode"] if raw.get("mode") in MODES else "unknown")
    elig_raw = raw.get("eligibility")
    eligibility = [
        s for s in (_str(e, ELIGIBILITY_ITEM_MAX) for e in (elig_raw or [])[:ELIGIBILITY_MAX_ITEMS])
        if s
    ] if isinstance(elig_raw, list) else []  # fmt: skip

    injected = any(
        _INJECTION.search(s or "") for s in (evidence, title, organiser, location, prize)
    )

    # timezone (A4.5)
    tz_name = raw.get("timezone_stated")
    tz = resolve_tz(tz_name)
    applied: str | None = str(tz) if tz else None
    if tz is None:
        tz = resolve_tz(default_tz)
        applied = default_tz if tz else None
    if tz is None:
        tz, applied = UTC, "UTC"

    evidence_dates = date_mentions(evidence)
    dates: dict[str, datetime | None] = {}
    naive_used = False
    for field, end_of_day, mentions in (
        ("registration_deadline", True, evidence_dates),
        ("event_start", False, evidence_dates | page_dates),
        ("event_end", True, evidence_dates | page_dates),
    ):
        value = raw.get(field)
        if value is None:
            dates[field] = None
            continue
        parsed = _parse(value, tz, end_of_day)
        if parsed is None or not (now.year - 1 <= parsed[1].year <= now.year + 5):
            reasons.append(f"{field}_invalid")
            dates[field] = None
            continue
        dt, day, naive = parsed
        if injected or not _mentioned(day, mentions):
            reasons.append(f"{field}_unsupported")
            dates[field] = None
            continue
        naive_used |= naive
        dates[field] = dt

    if naive_used and resolve_tz(tz_name) is None and resolve_tz(default_tz) is None:
        reasons.append("timezone_assumed_utc")

    deadline, start, end = dates["registration_deadline"], dates["event_start"], dates["event_end"]
    if deadline and deadline > now + MAX_DEADLINE_AHEAD:
        reasons.append("deadline_too_far")
    if deadline and start and start < deadline - START_BEFORE_DEADLINE_SLACK:
        reasons.append("start_before_deadline")
    if start and end and end < start:
        reasons.append("end_before_start")
        end = None

    url = _str(raw.get("registration_url"), URL_MAX)
    if url and (injected or not _url_ok(url, source_url, page)):
        url = None  # A5: drop, no review needed
    if injected:
        reasons.append("injection_suspected")

    return ExtractedCandidate(
        title=title,
        organiser=organiser,
        registration_url=url,
        registration_deadline=deadline,
        event_start=start,
        event_end=end,
        timezone_applied=applied,
        mode=mode,
        location=location,
        eligibility=eligibility,
        prize=prize,
        evidence=evidence,
        confidence=float(conf),
        status="closed" if deadline and deadline < now else "open",
        needs_review=bool(reasons),
        review_reasons=reasons,
        timezone_stated=_str(tz_name, 64),
        extractor=extractor,
    )


def postvalidate(
    data: dict[str, Any],
    *,
    page: PageText,
    source_url: str,
    now: datetime,
    extractor: str,
    default_tz: str | None = None,
) -> ExtractionResult:
    """Apply doc 08 A5 to one model output. `now` is the extraction time (UTC);
    `extractor` is `<provider>:<model>`."""
    is_hackathon = data.get("is_hackathon") is True
    raw_events = data.get("events")
    if not is_hackathon or not isinstance(raw_events, list):
        return ExtractionResult(is_hackathon=False, events=[])
    page_norm = _norm_ws(page.text)
    page_dates = date_mentions(page.text)
    events = [
        e
        for e in (
            _event(
                r,
                page=page,
                page_norm=page_norm,
                page_dates=page_dates,
                source_url=source_url,
                now=now,
                default_tz=default_tz,
                extractor=extractor,
            )
            for r in raw_events[:EVENTS_MAX]
            if isinstance(r, dict)
        )
        if e is not None
    ]
    return ExtractionResult(is_hackathon=True, events=events)
