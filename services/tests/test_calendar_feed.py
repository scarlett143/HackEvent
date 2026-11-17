"""HE-062: private calendar feed. Needs the test stack; see tests/stack.py."""

import re
import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from tests.stack import Actor, Pair, needs_stack, sql

from api.auth import hash_token

pytestmark = needs_stack

FEED = "/api/v1/me/calendar-feed"
NOW = datetime.now(UTC)


def event(title: str, *, status: str = "open") -> str:
    (eid,) = sql(
        "INSERT INTO events (title, organiser_name, description, registration_url,"
        " registration_deadline, event_start, mode, status, review_status, confidence, dedupe_key,"
        " first_seen_at) VALUES (:t, 'Org', 'About', 'https://example.com/r', :d, :d,"
        " 'online', CAST(:s AS event_status), 'auto', 0.9, :k, :f) RETURNING id::text",
        t=title,
        d=NOW + timedelta(days=5),
        s=status,
        k=uuid.uuid4().hex,
        f=NOW,
    )[0]
    return str(eid)


def track(who: Actor, eid: str) -> None:
    sql("INSERT INTO tracks (user_id, event_id) VALUES (CAST(:u AS uuid), CAST(:e AS uuid))", u=who.id, e=eid)


def register(who: Actor, eid: str) -> None:
    sql(
        "INSERT INTO registrations (user_id, event_id) VALUES (CAST(:u AS uuid), CAST(:e AS uuid))",
        u=who.id,
        e=eid,
    )


def subscribe(c: TestClient, who: Actor) -> str:
    r = c.post(FEED, headers=who.headers)
    assert r.status_code == 201, r.text
    return str(r.json()["url"])


def fetch(c: TestClient, url: str) -> tuple[int, str]:
    """Anonymous GET, exactly as a calendar app does it: no cookies, no CSRF."""
    path = re.sub(r"^https://[^/]+", "", url)
    r = c.get(path, headers={"Cookie": ""})
    return r.status_code, r.text


def test_management_endpoints_need_a_session(stack_client: TestClient) -> None:
    assert stack_client.get(FEED).status_code == 401
    # Anonymous writes are stopped by CSRF (403) or auth (401); either way nothing is created.
    assert stack_client.post(FEED).status_code in (401, 403)
    assert stack_client.delete(FEED).status_code in (401, 403)
    assert sql("SELECT count(*) FROM calendar_feeds")[0][0] == 0


def test_token_is_shown_once_and_only_its_hash_is_stored(stack_client: TestClient, two_users: Pair) -> None:
    a, _ = two_users
    assert stack_client.get(FEED, headers=a.headers).json() == {"enabled": False}
    url = subscribe(stack_client, a)
    token = re.search(r"/calendar/([A-Za-z0-9_-]+)\.ics$", url)
    assert token and len(token.group(1)) >= 40
    assert stack_client.get(FEED, headers=a.headers).json() == {"enabled": True}
    (stored,) = sql("SELECT token_hash FROM calendar_feeds WHERE user_id = CAST(:u AS uuid)", u=a.id)[0]
    assert stored == hash_token(token.group(1)) and token.group(1) not in stored


def test_feed_has_only_that_users_tracked_and_registered_events(stack_client: TestClient, two_users: Pair) -> None:
    a, b = two_users
    mine_tracked, mine_registered = event("A tracked"), event("A registered")
    cancelled, untracked, theirs = event("A cancelled", status="cancelled"), event("Nobody tracks me"), event("B only")
    track(a, mine_tracked)
    register(a, mine_registered)
    track(a, cancelled)
    track(b, theirs)
    code, body = fetch(stack_client, subscribe(stack_client, a))
    assert code == 200
    assert "A tracked" in body and "A registered" in body
    assert "A cancelled" not in body and "Nobody tracks me" not in body
    assert "B only" not in body  # cross-user: A's token never exposes B's rows
    code_b, body_b = fetch(stack_client, subscribe(stack_client, b))
    assert code_b == 200 and "B only" in body_b and "A tracked" not in body_b


def test_feed_response_is_calendar_data_and_not_cacheable_by_shared_caches(stack_client: TestClient, two_users: Pair) -> None:
    a, _ = two_users
    path = re.sub(r"^https://[^/]+", "", subscribe(stack_client, a))
    r = stack_client.get(path, headers={"Cookie": ""})
    assert r.headers["content-type"].startswith("text/calendar")
    assert "private" in r.headers["cache-control"]
    assert r.text.startswith("BEGIN:VCALENDAR\r\n") and r.text.endswith("END:VCALENDAR\r\n")


def test_rotate_kills_the_old_url_and_revoke_kills_them_all(stack_client: TestClient, two_users: Pair) -> None:
    a, _ = two_users
    track(a, event("Rotating"))
    old = subscribe(stack_client, a)
    assert fetch(stack_client, old)[0] == 200
    new = subscribe(stack_client, a)  # rotate
    assert new != old
    assert fetch(stack_client, old)[0] == 404
    assert fetch(stack_client, new)[0] == 200
    assert sql("SELECT count(*) FROM calendar_feeds WHERE user_id = CAST(:u AS uuid)", u=a.id)[0][0] == 1
    assert stack_client.delete(FEED, headers=a.headers).status_code == 204
    assert fetch(stack_client, new)[0] == 404
    assert stack_client.get(FEED, headers=a.headers).json() == {"enabled": False}


def test_bad_tokens_are_refused(stack_client: TestClient, two_users: Pair) -> None:
    random_token = "x" * 43
    assert stack_client.get(f"/api/v1/calendar/{random_token}.ics").status_code == 404
    assert stack_client.get("/api/v1/calendar/short.ics").status_code in (404, 422)
    assert stack_client.get(f"/api/v1/calendar/{'y' * 500}.ics").status_code in (404, 422)


def test_scraped_titles_cannot_inject_calendar_properties(stack_client: TestClient, two_users: Pair) -> None:
    a, _ = two_users
    track(a, event("Evil\r\nATTENDEE:mailto:x@example.com\r\nEND:VEVENT"))
    _, body = fetch(stack_client, subscribe(stack_client, a))
    lines = body.split("\r\n")
    assert not any(line.startswith("ATTENDEE") for line in lines)
    assert lines.count("BEGIN:VEVENT") == lines.count("END:VEVENT")
