"""iCalendar (RFC 5545) builder for the private calendar feed (HE-062).

Mirrors the browser export in apps/web/src/lib/calendar.ts: per event a 30-minute "Registration
closes" entry ending at the deadline, and the event's own run (start..end, default 1 h). Event text
is scraped, so it is untrusted: every TEXT value is escaped and long lines are folded, so a title
can never inject extra properties or lines.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

PRODID = "-//HackEvents//EN"


@dataclass(frozen=True)
class FeedEvent:
    id: str
    title: str
    registration_deadline: datetime | None
    event_start: datetime | None
    event_end: datetime | None
    location: str | None


def stamp(dt: datetime) -> str:
    """20261119T181500Z (UTC)."""
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def escape(text: str) -> str:
    """RFC 5545 section 3.3.11 TEXT escaping; newlines become the two characters backslash-n."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def fold(line: str) -> str:
    """Fold at 75 octets (continuation lines: CRLF + space, 74 octets of content). Never splits a
    multi-byte character."""
    out: list[str] = []
    cur = ""
    size = 0
    for ch in line:
        n = len(ch.encode())
        if size + n > (74 if out else 75):
            out.append(cur)
            cur, size = "", 0
        cur += ch
        size += n
    out.append(cur)
    return "\r\n ".join(out)


def _slots(ev: FeedEvent) -> list[tuple[str, str, datetime, datetime, str | None]]:
    """(uid, summary, start, end, location) entries for one event."""
    out = []
    if ev.registration_deadline:
        end = ev.registration_deadline
        out.append(
            (f"{ev.id}-deadline", f"Registration closes: {ev.title}", end - timedelta(minutes=30), end, None)
        )
    if ev.event_start:
        start = ev.event_start
        end = ev.event_end if ev.event_end and ev.event_end > start else start + timedelta(hours=1)
        out.append((f"{ev.id}-event", ev.title, start, end, ev.location))
    return out


def build(events: Iterable[FeedEvent], *, name: str, now: datetime | None = None) -> str:
    """A complete VCALENDAR. `now` is injectable for tests."""
    now = now or datetime.now(UTC)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape(name)}",
        "X-PUBLISHED-TTL:PT1H",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
    ]
    for ev in events:
        for uid, summary, start, end, location in _slots(ev):
            lines += [
                "BEGIN:VEVENT",
                f"UID:{uid}@hackevents",
                f"DTSTAMP:{stamp(now)}",
                f"DTSTART:{stamp(start)}",
                f"DTEND:{stamp(end)}",
                f"SUMMARY:{escape(summary)}",
            ]
            if location:
                lines.append(f"LOCATION:{escape(location)}")
            lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(line) for line in lines) + "\r\n"
