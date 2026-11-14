"""HE-023..025: structured source adapters against saved real fixtures (no network).

Fixture provenance (fetched 2026-11-14 with User-Agent HackEventsBot/0.1):
- rss/mlh_blog.xml: https://news.mlh.io/feed (302 -> https://blog.mlh.com/feed); trimmed to
  3 items, dc:creator names replaced, items mentioning individual users dropped.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from workers.scrape.adapters import ADAPTERS, EventCandidate, ParseError, UnknownAdapter, parse

FIXTURES = Path(__file__).parent / "fixtures" / "adapters"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def assert_plain(c: EventCandidate) -> None:
    for text in (c.title, c.organiser, c.description, c.prize, c.location, *c.eligibility):
        assert text is None or ("<" not in text and "&amp;" not in text), text


# --- shared candidate model ---------------------------------------------------------------


def test_candidate_strips_html_caps_and_drops_bad_urls() -> None:
    c = EventCandidate(
        title="<b>Hack</b> &amp; <script>alert(1)</script>Build" + "x" * 300,
        organiser="Org",
        registration_url="javascript:alert(1)",
        description="<p>one</p><p>two&nbsp;<a href='x'>link</a></p>",
        eligibility=["<i>Students</i>", "Students", "y" * 80],
        confidence=1,
        extractor="test",
    )
    assert c.title.startswith("Hack & Buildxxx") and len(c.title) == 200
    assert c.registration_url is None
    assert c.description == "one\ntwo link"
    assert c.eligibility == ["Students", "y" * 40]


def test_candidate_rejects_naive_datetimes_and_normalises_to_utc() -> None:
    with pytest.raises(ValueError):
        EventCandidate(
            title="t", organiser="o", event_start=datetime(2026, 1, 1), confidence=1, extractor="x"
        )
    ist = datetime.fromisoformat("2026-12-20T00:00:00+05:30")
    c = EventCandidate(
        title="t", organiser="o", registration_deadline=ist, confidence=1, extractor="x"
    )
    assert c.registration_deadline == datetime(2026, 12, 19, 18, 30, tzinfo=UTC)
    assert c.registration_deadline.utcoffset() is not None
    assert c.registration_deadline.tzinfo is UTC


# --- dispatch ------------------------------------------------------------------------------


def test_parse_unknown_adapter() -> None:
    with pytest.raises(UnknownAdapter):
        parse("myspace", b"", "https://example.com/")


def test_parse_dispatches_to_registered_adapter() -> None:
    assert parse("rss", fixture("rss/mlh_blog.xml"), "https://blog.mlh.com/feed") == ADAPTERS[
        "rss"
    ](fixture("rss/mlh_blog.xml"), "https://blog.mlh.com/feed")


# --- HE-023 RSS / Atom ---------------------------------------------------------------------


def test_rss_fixture() -> None:
    out = parse("rss", fixture("rss/mlh_blog.xml"), "https://blog.mlh.com/feed")
    assert len(out) == 3
    first = out[0]
    assert first.title == "Can you merge at agent speed?"
    assert first.organiser == "DEV Community: Major League Hacking (MLH)"
    assert first.registration_url == "https://blog.mlh.com/can-you-merge-at-agent-speed-4m67"
    assert first.description and first.description.startswith("Major League Hacking and Entire")
    assert first.extractor == "rss" and first.mode == "unknown"
    # pubDate is publication time, never an event date or deadline.
    assert all(c.registration_deadline is None and c.event_start is None for c in out)
    for c in out:
        assert_plain(c)


ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Org Events</title>
  <entry>
    <title type="html">Spring &lt;em&gt;Hack&lt;/em&gt; 2027</title>
    <link rel="self" href="https://example.com/self"/>
    <link rel="alternate" href="/events/spring-hack"/>
    <summary type="html">&lt;p&gt;Register by &lt;b&gt;March 1&lt;/b&gt;&lt;/p&gt;</summary>
  </entry>
  <entry><title></title><link href="https://example.com/untitled"/></entry>
  <entry><title>Bad link</title><link href="ftp://example.com/x"/></entry>
</feed>"""


def test_atom_feed() -> None:
    out = parse("rss", ATOM, "https://example.com/feed.atom")
    assert [c.title for c in out] == ["Spring Hack 2027", "Bad link"]  # untitled entry skipped
    assert out[0].organiser == "Example Org Events"
    assert out[0].registration_url == "https://example.com/events/spring-hack"
    assert out[0].description == "Register by March 1"
    assert out[1].registration_url is None


BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>
<rss><channel><title>&lol3;</title></channel></rss>"""

XXE = b"""<?xml version="1.0"?>
<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<rss><channel><title>t</title><item><title>&xxe;</title></item></channel></rss>"""

XXE_PARAM = b"""<?xml version="1.0"?>
<!DOCTYPE rss SYSTEM "http://169.254.169.254/evil.dtd">
<rss><channel><title>t</title></channel></rss>"""


@pytest.mark.parametrize("body", [BILLION_LAUGHS, XXE, XXE_PARAM])
def test_rss_rejects_dtd_entities(body: bytes) -> None:
    with pytest.raises(ParseError, match="not allowed"):
        parse("rss", body, "https://example.com/feed")


@pytest.mark.parametrize(
    "body", [b"", b"not xml", b"<rss><channel>", b"<html><body>hi</body></html>", b"\xff\xfe\x00"]
)
def test_rss_malformed_is_parse_error(body: bytes) -> None:
    with pytest.raises(ParseError):
        parse("rss", body, "https://example.com/feed")
