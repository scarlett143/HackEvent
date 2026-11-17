"""HE-062: the .ics builder. Pure, no stack needed."""

from datetime import UTC, datetime, timedelta, timezone

from common import ics

NOW = datetime(2026, 11, 17, tzinfo=UTC)
IST = timezone(timedelta(hours=5, minutes=30))


def ev(**over: object) -> ics.FeedEvent:
    base = dict(
        id="e1",
        title="Hack, Build; Ship\nNow",
        registration_deadline=datetime(2026, 11, 19, 18, 29, tzinfo=UTC),
        event_start=datetime(2026, 11, 21, 4, 30, tzinfo=UTC),
        event_end=datetime(2026, 11, 23, 12, 0, tzinfo=UTC),
        location="Global Online",
    )
    return ics.FeedEvent(**(base | over))  # type: ignore[arg-type]


def test_stamp_is_utc_even_for_other_zones() -> None:
    assert ics.stamp(datetime(2026, 11, 19, 23, 59, tzinfo=IST)) == "20261119T182900Z"


def test_escape_text_specials() -> None:
    assert ics.escape("a,b;c\\d\ne\r\nf") == "a\\,b\\;c\\\\d\\ne\\nf"


def test_fold_limits_octets_and_never_splits_a_character() -> None:
    line = "X:" + "é" * 80
    folded = ics.fold(line)
    assert all(len(part.encode()) <= 75 for part in folded.split("\r\n"))
    assert folded.replace("\r\n ", "") == line


def test_calendar_has_a_deadline_slot_and_an_event_slot() -> None:
    out = ics.build([ev()], name="HackEvents", now=NOW)
    assert out.startswith("BEGIN:VCALENDAR\r\n") and out.endswith("END:VCALENDAR\r\n")
    assert out.count("BEGIN:VEVENT") == 2
    assert "UID:e1-deadline@hackevents" in out
    assert "DTSTART:20261119T175900Z" in out  # deadline minus 30 min
    assert "DTEND:20261123T120000Z" in out
    assert "LOCATION:Global Online" in out
    assert "X-WR-CALNAME:HackEvents" in out


def test_scraped_title_cannot_inject_properties_or_lines() -> None:
    evil = ev(title="x\r\nATTENDEE:mailto:evil@example.com\r\nEND:VEVENT")
    out = ics.build([evil], name="n", now=NOW)
    lines = out.split("\r\n")
    assert not any(line.startswith("ATTENDEE") for line in lines)
    # Count whole lines: the escaped text stays inside the SUMMARY line as literal backslash-n.
    assert lines.count("BEGIN:VEVENT") == lines.count("END:VEVENT") == 2
    unfolded = out.replace("\r\n ", "").split("\r\n")
    assert next(line for line in unfolded if line.startswith("SUMMARY:")).count("\\n") == 2


def test_missing_dates_are_skipped_and_missing_end_defaults_to_one_hour() -> None:
    none = ev(registration_deadline=None, event_start=None)
    assert "BEGIN:VEVENT" not in ics.build([none], name="n", now=NOW)
    no_end = ev(registration_deadline=None, event_end=None)
    assert "DTEND:20261121T053000Z" in ics.build([no_end], name="n", now=NOW)
