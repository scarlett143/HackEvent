"""HE-023..025: structured source adapters against saved real fixtures (no network).

Fixture provenance (fetched 2026-11-14 with User-Agent HackEventsBot/0.1):
- rss/mlh_blog.xml: https://news.mlh.io/feed (302 -> https://blog.mlh.com/feed); trimmed to
  3 items, dc:creator names replaced, items mentioning individual users dropped.
- devpost/hackathons_open_upcoming_page3.json: verbatim response of
  https://devpost.com/api/hackathons?status[]=upcoming&status[]=open&page=3 (9 items).
- mlh/mlh_season_2027.html: https://www.mlh.com/seasons/2027/events (fetched 2026-11-18;
  mlh.io redirects there). Shell reduced to the Inertia data-page script; props kept
  verbatim except upcomingEvents cut to 6, pastEvents to 2, navigation emptied.
- reddit/hackathons_new.json: SYNTHETIC (anonymous Reddit access is refused, so no real
  response could be saved). Shape follows the documented Listing / t3 schema; example
  names only. Covers the skip rules: stickied, NSFW, removed, no keyword, non-post child.
- substack/substack_ai_engineer.xml: https://aiengineer.substack.com/feed (fetched 2026-11-18);
  trimmed to 3 items, each content:encoded cut to its first paragraph, author names and
  emails replaced with placeholders.
- unstop/hackathons_open_page1.json: response of https://unstop.com/api/public/opportunity/
  search-result?opportunity=hackathons&page=1&per_page=10&oppstatus=open (10 items); only
  change: each `details` HTML string cut to its first 1500 characters to keep the file small.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from workers.scrape.adapters import ADAPTERS, EventCandidate, ParseError, UnknownAdapter, parse
from workers.scrape.adapters.devpost import submission_period

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
        description="<p>one</p><p>two&nbsp;<a href='x'>link</a></p> 1 < 2 <a href='trunc",
        eligibility=["<i>Students</i>", "Students", "y" * 80],
        confidence=1,
        extractor="test",
    )
    assert c.title.startswith("Hack & Buildxxx") and len(c.title) == 200
    assert c.registration_url is None
    assert c.description == "one\ntwo link\n1 < 2"  # plain-text "<" kept; cut-off tag dropped
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
  <entry><title>Bad link hackathon</title><link href="ftp://example.com/x"/></entry>
  <entry><title>Quarterly report</title><summary>&lt;a href="https://hack.example"&gt;x&lt;/a&gt;</summary></entry>
</feed>"""


def test_atom_feed() -> None:
    out = parse("rss", ATOM, "https://example.com/feed.atom")
    # untitled entry skipped; no keyword in the text (only in a link) -> not a hackathon
    assert [c.title for c in out] == ["Spring Hack 2027", "Bad link hackathon"]
    assert out[0].organiser == "Example Org Events"
    assert out[0].registration_url == "https://example.com/events/spring-hack"
    assert out[0].description == "Register by March 1"
    assert out[1].registration_url is None


def test_substack_feed_keeps_only_keyword_items() -> None:
    """HE-029: Substack /feed is RSS 2.0 (CDATA, content:encoded, itunes namespaces)."""
    url = "https://aiengineer.substack.com/feed"
    out = parse("rss", fixture("substack/substack_ai_engineer.xml"), url)
    # 3 posts; only the one whose text has a doc 08 keyword ("Hackers") survives. The
    # LLM step on the linked page is what rejects false positives like this one.
    assert [c.title for c in out] == [
        "Your Agent Hallucinates Packages. Hackers Just Weaponised That."
    ]
    c = out[0]
    assert c.organiser == "The AI Engineer"
    assert c.registration_url and c.registration_url.startswith(
        "https://aiengineer.substack.com/p/"
    )
    assert c.confidence == 0.5 and c.registration_deadline is None
    assert_plain(c)


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


# --- HE-024 Devpost ------------------------------------------------------------------------

DEVPOST_URL = "https://devpost.com/api/hackathons?status[]=upcoming&status[]=open&page=3"


def test_devpost_fixture() -> None:
    out = parse("devpost", fixture("devpost/hackathons_open_upcoming_page3.json"), DEVPOST_URL)
    assert len(out) == 9
    by_title = {c.title: c for c in out}
    assert len(by_title) == 9
    for c in out:
        assert_plain(c)
        assert c.extractor == "devpost"
        assert c.registration_deadline is None  # listing has no time/timezone: never guessed
        assert c.registration_url and c.registration_url.startswith("https://")
        assert c.event_start and c.event_end and c.event_start <= c.event_end
    in_person = [c for c in out if c.mode == "offline"]
    assert [(c.title, c.location) for c in in_person] == [
        ("University Blockchain Conference Hackathon 2026", "University of Texas at Austin")
    ]
    assert sum(c.mode == "online" for c in out) == 8
    ubc = in_person[0]
    assert ubc.event_start == datetime(2026, 11, 13, tzinfo=UTC)  # "Nov 13 - 20, 2026"
    assert ubc.event_end == datetime(2026, 11, 20, tzinfo=UTC)
    assert ubc.eligibility == ["Invite only"]
    assert ubc.prize == "$22,000"


def test_devpost_dates_and_prizes() -> None:
    out = parse("devpost", fixture("devpost/hackathons_open_upcoming_page3.json"), DEVPOST_URL)
    first = out[0]
    assert first.organiser == "SafeSpace"
    assert first.registration_url == "https://safespace-hacks.devpost.com/"
    assert first.prize == "$280"
    # "Oct 17 - Nov 22, 2026" -> calendar dates at 00:00 UTC, time not stated.
    assert first.event_start == datetime(2026, 10, 17, tzinfo=UTC)
    assert first.event_end == datetime(2026, 11, 22, tzinfo=UTC)
    assert first.timezone_stated is None
    assert out[1].prize is None  # "$0"
    # Cross-year range: "Jul 18, 2026 - Jan 14, 2027".
    assert out[1].event_start == datetime(2026, 7, 18, tzinfo=UTC)
    assert out[1].event_end == datetime(2027, 1, 14, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "start", "end"),
    [
        ("Nov 11 - 30, 2026", (2026, 11, 11), (2026, 11, 30)),
        ("Dec 20 - Jan 05, 2027", (2026, 12, 20), (2027, 1, 5)),
        ("Nov 14, 2026", (2026, 11, 14), (2026, 11, 14)),
        ("Feb 30 - Mar 02, 2027", None, None),
        ("Soon", None, None),
        ("", None, None),
    ],
)
def test_devpost_submission_period(
    text: str, start: tuple[int, int, int] | None, end: tuple[int, int, int] | None
) -> None:
    got = submission_period(text)
    want = tuple(datetime(*d, tzinfo=UTC) if d else None for d in (start, end))
    assert got == want


@pytest.mark.parametrize("body", [b"", b"[]", b'{"hackathons": 3}', b"{bad json", b'{"x": []}'])
def test_devpost_malformed_is_parse_error(body: bytes) -> None:
    with pytest.raises(ParseError):
        parse("devpost", body, DEVPOST_URL)


def test_devpost_skips_bad_items() -> None:
    body = (
        b'{"hackathons": [null, {"title": "No org"}, {"title": "Ended", "organization_name": "O",'
        b' "open_state": "ended"}, {"title": "<b>Ok</b>", "organization_name": "O",'
        b' "url": "javascript:x", "submission_period_dates": "garbage"}]}'
    )
    [only] = parse("devpost", body, DEVPOST_URL)
    assert only.title == "Ok" and only.registration_url is None and only.event_start is None


# --- HE-025 Unstop -------------------------------------------------------------------------

UNSTOP_URL = (
    "https://unstop.com/api/public/opportunity/search-result"
    "?opportunity=hackathons&page=1&per_page=10&oppstatus=open"
)


def test_unstop_fixture() -> None:
    out = parse("unstop", fixture("unstop/hackathons_open_page1.json"), UNSTOP_URL)
    assert len(out) == 10
    for c in out:
        assert_plain(c)
        assert c.extractor == "unstop"
        assert c.registration_deadline is not None and c.registration_deadline.tzinfo is UTC
        assert c.event_start is None  # listing has no start date; never copied from deadline
    assert [c.mode for c in out].count("offline") == 4
    assert [c.mode for c in out].count("online") == 6

    first = out[0]
    assert first.title == "Mantrastra"
    assert first.organiser == "Madan Mohan Malaviya University of Technology"
    assert first.registration_url == (
        "https://unstop.com/hackathons/"
        "mantrastra-techsrijan-27-madan-mohan-malaviya-university-of-technology-1810554"
    )
    # end_regn_dt "2026-12-20T00:00:00+05:30" (IST midnight) -> previous day 18:30 UTC.
    assert first.registration_deadline == datetime(2026, 12, 19, 18, 30, tzinfo=UTC)
    assert first.event_end == datetime(2026, 12, 26, 14, 0, tzinfo=UTC)
    assert first.timezone_stated == "UTC+05:30"
    assert first.location == "Gorakhpur, Uttar Pradesh, India"
    assert first.eligibility[:2] == ["Engineering Students", "Postgraduate"]
    assert first.prize == "INR 12,000 total"
    assert first.description and first.description.startswith("About the Competition\nMANTRASTRA")

    seed = out[2]
    assert seed.mode == "online" and seed.location is None
    assert seed.registration_deadline == datetime(2026, 11, 22, 18, 29, 40, tzinfo=UTC)


def test_unstop_ambiguous_deadline_is_none() -> None:
    body = (
        b'{"data": {"data": [{"title": "T", "organisation": {"name": "O"}, "region": "x",'
        b' "regnRequirements": {"end_regn_dt": "2026-12-20 00:00:00"},'
        b' "public_url": "hackathons/t-1", "details": "<script>x()</script><b>Hi</b>"},'
        b' {"title": "", "organisation": {"name": "O"}}, 7]}}'
    )
    [only] = parse("unstop", body, UNSTOP_URL)
    assert only.registration_deadline is None and only.timezone_stated is None
    assert only.registration_url == "https://unstop.com/hackathons/t-1"
    assert only.mode == "unknown"
    assert only.description == "Hi"


@pytest.mark.parametrize("body", [b"", b"[]", b'{"data": {"data": {}}}', b"{nope", b'{"data": 1}'])
def test_unstop_malformed_is_parse_error(body: bytes) -> None:
    with pytest.raises(ParseError):
        parse("unstop", body, UNSTOP_URL)


# --- HE-026 MLH ----------------------------------------------------------------------------


MLH_URL = "https://www.mlh.com/seasons/2027/events"


def test_mlh_fixture() -> None:
    out = parse("mlh", fixture("mlh/mlh_season_2027.html"), MLH_URL)
    assert len(out) == 6  # 6 upcoming; the 2 pastEvents are never read
    first = out[0]
    assert first.title == "Hack Western 13"
    assert first.organiser == "Major League Hacking (MLH)"
    assert first.registration_url == "https://hackwestern.com"
    assert first.event_start == datetime(2026, 11, 20, 23, 0, tzinfo=UTC)
    assert first.event_end == datetime(2026, 11, 22, 20, 0, tzinfo=UTC)
    assert first.registration_deadline is None  # MLH lists none
    assert first.mode == "offline" and first.location == "London, Ontario"
    assert first.extractor == "mlh" and first.confidence == 1.0
    assert "NOV 20 - 22" in first.evidence
    for c in out:
        assert_plain(c)


def test_mlh_modes_ended_and_fallback_url() -> None:
    import json

    events = [
        {
            "name": "A",
            "status": "pending",
            "formatType": "hybrid_physical",
            "location": "X",
            "url": "/events/a/prizes",
            "startsAt": "2027-01-01T10:00:00Z",
        },
        {
            "name": "B",
            "status": "pending",
            "formatType": "digital",
            "location": "Everywhere",
            "organizationName": "Org B",
        },
        {"name": "C", "status": "ended", "formatType": "physical"},
        {"name": "", "status": "pending"},  # invalid item skipped, not fatal
    ]
    page = {"props": {"upcomingEvents": events}}
    body = f'<script data-page="app" type="application/json">{json.dumps(page)}</script>'
    out = parse("mlh", body.encode(), MLH_URL)
    assert [c.title for c in out] == ["A", "B"]
    assert (
        out[0].mode == "hybrid" and out[0].registration_url == "https://www.mlh.com/events/a/prizes"
    )
    assert out[1].mode == "online" and out[1].location is None and out[1].organiser == "Org B"


@pytest.mark.parametrize(
    "body",
    [
        b"<html>no data</html>",
        b'<script data-page="app" type="application/json">{not json</script>',
        b'<script data-page="app" type="application/json">{"props": {}}</script>',
    ],
)
def test_mlh_malformed_is_parse_error(body: bytes) -> None:
    with pytest.raises(ParseError):
        parse("mlh", body, MLH_URL)


# --- HE-028 Reddit -------------------------------------------------------------------------


REDDIT_URL = "https://oauth.reddit.com/r/hackathons/new?limit=50&raw_json=1"


def test_reddit_fixture() -> None:
    out = parse("reddit", fixture("reddit/hackathons_new.json"), REDDIT_URL)
    # stickied, no-keyword, NSFW, removed posts and the comment child are skipped
    assert [c.title for c in out] == [
        "[Online] Example Accessibility Hackathon, apply by Dec 15",
        "Example Corp Global Buildathon 2027",
    ]
    self_post, link_post = out
    assert self_post.registration_url == (
        "https://www.reddit.com/r/hackathons/comments/p2/x/"
    )  # self post -> the thread
    assert self_post.description and "Applications close December 15." in self_post.description
    assert link_post.registration_url == "https://buildathon.example.com/2027"
    for c in out:
        assert c.organiser == "r/hackathons" and c.extractor == "reddit"
        assert c.confidence == 0.5 and c.registration_deadline is None and c.event_start is None
        assert "example_user" not in (c.description or "") + c.evidence  # no author names
        assert_plain(c)


@pytest.mark.parametrize(
    "body", [b"{}", b"not json", b'{"kind": "t3", "data": {"children": []}}', b"[]"]
)
def test_reddit_malformed_is_parse_error(body: bytes) -> None:
    with pytest.raises(ParseError):
        parse("reddit", body, REDDIT_URL)
