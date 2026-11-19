"""HE-050 events API (+ HE-054/055 API part). Acceptance: p95 < 300 ms on 5k events.

Stack tests need a THROWAWAY Postgres + Redis (see tests/stack.py); `events` and
`organisations` are truncated. The performance test runs wherever the stack does (CI has it).
"""

import statistics
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.stack import Actor, Pair, make_actor, needs_stack, sql

from api.events import plain_text

NOW = datetime.now(UTC)
URL = "/api/v1/events"
ALL = {"scope": "all"}  # most tests use unmatched events; priority scope has its own test


def test_plain_text_strips_tags_and_escaped_tags() -> None:
    assert plain_text("<p>Closes <b>20 Nov</b></p>") == "Closes 20 Nov"
    assert plain_text("&lt;script&gt;alert(1)&lt;/script&gt; ok") == "alert(1) ok"
    assert plain_text("&amp;lt;img src=x onerror=1&amp;gt;") == ""
    assert plain_text("prize > 1 lakh & 2 < 3") == "prize > 1 lakh & 2 < 3"
    assert plain_text("<img src=x onerror=alert(1)") == ""


# --- seeding -----------------------------------------------------------------------------


@pytest.fixture
def clean(stack_client: TestClient) -> TestClient:
    sql("TRUNCATE events, organisations CASCADE")
    return stack_client


def org(name: str, kind: str = "company", tier: int = 1, region: str = "india") -> str:
    (oid,) = sql(
        "INSERT INTO organisations (name, kind, region, tier, rank_source, website, is_active)"
        " VALUES (:n, CAST(:k AS org_kind), CAST(:r AS org_region), :t, 'test', 'https://x', true)"
        " RETURNING id::text",
        n=name,
        k=kind,
        r=region,
        t=tier,
    )[0]
    return str(oid)


def event(
    title: str,
    deadline: datetime | None = NOW + timedelta(days=30),
    *,
    org_id: str | None = None,
    mode: str = "online",
    status: str = "open",
    review: str = "auto",
    first_seen: datetime = NOW - timedelta(days=1),
    snippet: str | None = None,
) -> str:
    (eid,) = sql(
        "INSERT INTO events (title, organiser_name, organisation_id, description,"
        " registration_url, registration_deadline, event_start, mode, status, review_status,"
        " confidence, dedupe_key, first_seen_at) VALUES (:t, 'Org', CAST(:o AS uuid),"
        " '<p>About</p>', 'https://example.com/r', :d, :d, CAST(:m AS event_mode),"
        " CAST(:s AS event_status), CAST(:rv AS review_status), 0.9, :k, :f)"
        " RETURNING id::text",
        t=title,
        o=org_id,
        d=deadline,
        m=mode,
        s=status,
        rv=review,
        k=uuid.uuid4().hex,
        f=first_seen,
    )[0]
    if snippet is not None:
        sql(
            "INSERT INTO event_sources (event_id, source_url, snippet, extracted, extractor)"
            " VALUES (CAST(:e AS uuid), 'https://www.devpost.com/h', :s, '{}', 'devpost')",
            e=eid,
            s=snippet,
        )
    return str(eid)


def ids(c: TestClient, who: Actor, **params: Any) -> list[str]:
    r = c.get(URL, headers=who.headers, params={**ALL, **params})
    assert r.status_code == 200, r.text
    return [e["id"] for e in r.json()["items"]]


# --- list --------------------------------------------------------------------------------


@needs_stack
def test_default_hides_closed_cancelled_rejected_and_sorts_by_deadline(
    clean: TestClient, two_users: Pair
) -> None:
    a, _ = two_users
    later = event("Later", NOW + timedelta(days=20))
    soon = event("Soon", NOW + timedelta(days=2))
    no_deadline = event("No deadline", None)
    event("Closed", NOW + timedelta(days=5), status="closed")
    stale = event("Past deadline, status not refreshed", NOW - timedelta(hours=1))
    event("Cancelled", NOW + timedelta(days=5), status="cancelled")
    event("Rejected", NOW + timedelta(days=5), review="rejected")
    review = event("Needs review", NOW + timedelta(days=10), review="needs_review")
    assert ids(clean, a) == [soon, review, later, no_deadline]
    closed = clean.get(URL, headers=a.headers, params={**ALL, "status": "closed"}).json()["items"]
    assert {e["title"] for e in closed} == {"Closed", "Past deadline, status not refreshed"}
    assert {e["status"] for e in closed} == {"closed"}
    assert stale in ids(clean, a, status="open,closed")
    assert [
        e["title"]
        for e in clean.get(URL, headers=a.headers, params={**ALL, "status": "cancelled"}).json()[
            "items"
        ]
    ] == ["Cancelled"]
    assert ids(clean, a, status="closing") == [soon]


@needs_stack
def test_each_filter(clean: TestClient, two_users: Pair) -> None:
    a, _ = two_users
    co = org("Example Corp", "company", 1, "india")
    inst = org("Example Institute", "institution", 2, "global")
    e_co = event("Corp Hack", NOW + timedelta(days=3), org_id=co, mode="online")
    e_inst = event("Institute Hack", NOW + timedelta(days=10), org_id=inst, mode="offline")
    e_none = event("Unmatched 100%_hack", NOW + timedelta(days=40), mode="hybrid")
    # e_none's organiser ("Org") matched nothing and has no college word → guessed company
    assert ids(clean, a, kind="company") == [e_co, e_none]
    assert ids(clean, a, kind="institution") == [e_inst]
    assert ids(clean, a, kind="institution,company") == [e_co, e_inst, e_none]
    assert ids(clean, a, tier="2") == [e_inst]
    # e_none has no organisation, no location, no Unstop source → "global" (outside India)
    assert ids(clean, a, region="global") == [e_inst, e_none]
    assert ids(clean, a, region="india") == [e_co]
    assert ids(clean, a, mode="hybrid") == [e_none]
    assert ids(clean, a, mode="online,offline") == [e_co, e_inst]
    assert ids(clean, a, deadline_before=(NOW + timedelta(days=5)).isoformat()) == [e_co]
    assert ids(clean, a, deadline_after=(NOW + timedelta(days=5)).isoformat()) == [e_inst, e_none]
    assert ids(clean, a, q="institute") == [e_inst]
    assert ids(clean, a, q="100%_") == [e_none]
    assert ids(clean, a, q="Corp_Hack") == []  # LIKE wildcards are literal
    assert clean.post(f"{URL}/{e_inst}/registration", headers=a.headers).status_code == 200
    assert ids(clean, a, registered="true") == [e_inst]
    assert ids(clean, a, registered="false") == [e_co, e_none]
    org_out = clean.get(URL, headers=a.headers, params={**ALL, "kind": "institution"}).json()[
        "items"
    ][0]
    assert org_out["organiser"] == {
        "name": "Example Institute",
        "id": inst,
        "kind": "institution",
        "tier": 2,
        "region": "global",
    }
    unmatched = clean.get(URL, headers=a.headers, params={**ALL, "mode": "hybrid"}).json()["items"][
        0
    ]
    assert unmatched["organiser"] == {
        "name": "Org",
        "id": None,
        "kind": "company",
        "tier": None,
        "region": None,
    }


@needs_stack
def test_region_falls_back_to_location_and_unstop_source(
    clean: TestClient, two_users: Pair
) -> None:
    # Scraped events usually match no seeded organisation; India/Global must still work.
    a, _ = two_users
    glob = org("Global Corp", "company", 1, "global")
    by_location = event("Kolkata Hack", NOW + timedelta(days=2))
    sql(
        "UPDATE events SET location = 'Kolkata, West Bengal, India' WHERE id = CAST(:e AS uuid)",
        e=by_location,
    )
    by_unstop = event("Unstop Hack", NOW + timedelta(days=3))
    sql(
        "INSERT INTO event_sources (event_id, source_url, snippet, extracted, extractor)"
        " VALUES (CAST(:e AS uuid), 'https://unstop.com/hackathons/x-123', 's', '{}', 'unstop')",
        e=by_unstop,
    )
    devpost_online = event("Devpost Online Hack", NOW + timedelta(days=4), snippet="s")
    org_wins = event("Global Org In India", NOW + timedelta(days=5), org_id=glob)
    sql("UPDATE events SET location = 'Pune, India' WHERE id = CAST(:e AS uuid)", e=org_wins)
    assert ids(clean, a, region="india") == [by_location, by_unstop]
    assert ids(clean, a, region="global") == [devpost_online, org_wins]
    assert len(ids(clean, a, region="india,global")) == 4


@needs_stack
def test_bad_filters_and_cursor_use_doc06_error_shape(clean: TestClient, two_users: Pair) -> None:
    a, _ = two_users
    for params in ({"status": "bogus"}, {"tier": "4"}, {"kind": "x"}, {"mode": ""}):
        r = clean.get(URL, headers=a.headers, params={**ALL, **params})
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_filter", params
    r = clean.get(URL, headers=a.headers, params={**ALL, "cursor": "not-a-cursor"})
    assert r.status_code == 400
    assert r.json() == {
        "error": {"code": "invalid_cursor", "message": "Invalid cursor.", "details": {}}
    }
    r = clean.get(URL, headers=a.headers, params={**ALL, "limit": 0})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"
    assert clean.get(URL).status_code == 401


@needs_stack
def test_new_only_unread_count_and_seen(clean: TestClient, two_users: Pair) -> None:
    a, b = two_users
    p = org("Example Institute", "institution", 1)  # unread-count counts priority events only
    old = event("Old", first_seen=NOW - timedelta(days=3), org_id=p)
    event("Closed new", status="closed", first_seen=NOW, org_id=p)
    event("Not priority", first_seen=NOW)  # no matched organisation: never counted
    # Never visited: everything open is new.
    assert clean.get("/api/v1/me/unread-count", headers=a.headers).json() == {
        "new_events": 1,
        "notifications": 0,
    }
    seen = clean.post("/api/v1/me/seen", headers=a.headers)
    assert seen.status_code == 200 and seen.json()["last_seen_at"].endswith("Z")
    # HE-052 acceptance: the badge clears after seen.
    assert clean.get("/api/v1/me/unread-count", headers=a.headers).json()["new_events"] == 0
    assert ids(clean, a, new_only="true") == []
    fresh = event("Fresh", first_seen=datetime.now(UTC) + timedelta(seconds=1), org_id=p)
    assert ids(clean, a, new_only="true", scope="priority") == [fresh]
    flags = {e["id"]: e["is_new"] for e in clean.get(URL, headers=a.headers).json()["items"]}
    assert flags == {old: False, fresh: True}
    for who, n in ((a, 2), (b, 1)):
        for _ in range(n):
            sql(
                "INSERT INTO notifications (user_id, type, title, body)"
                " VALUES (CAST(:u AS uuid), 'reminder', 't', 'b')",
                u=who.id,
            )
    sql("UPDATE notifications SET read_at = now() WHERE id = (SELECT id FROM notifications"
        " WHERE user_id = CAST(:u AS uuid) LIMIT 1)", u=a.id)  # fmt: skip
    assert clean.get("/api/v1/me/unread-count", headers=a.headers).json() == {
        "new_events": 1,
        "notifications": 1,
    }
    # B never called /me/seen: A's visit doesn't touch B.
    assert clean.get("/api/v1/me/unread-count", headers=b.headers).json()["new_events"] == 2


@needs_stack
def test_cursor_paging_is_stable_with_ties(clean: TestClient, two_users: Pair) -> None:
    a, _ = two_users
    tie = NOW + timedelta(days=4)
    expected = sorted(event(f"Tie {i}", tie) for i in range(5))
    expected = [event("First", NOW + timedelta(days=1)), *expected]
    expected += sorted(event(f"Null {i}", None) for i in range(4))
    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = clean.get(URL, headers=a.headers, params={**ALL, **params}).json()
        seen += [e["id"] for e in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
        # A row inserted ahead of the cursor mid-scroll doesn't shift later pages.
        if len(seen) == 2:
            event("Inserted earlier", NOW + timedelta(hours=1))
    assert seen == expected


# --- detail, sources, timezone -----------------------------------------------------------


@needs_stack
def test_detail_source_block_plain_text_utc(clean: TestClient, two_users: Pair) -> None:
    a, _ = two_users
    eid = event(
        "<b>Example</b> Hack",
        datetime(2027, 1, 5, 18, 29, tzinfo=UTC),
        review="needs_review",
        snippet='<p onclick="x()">Registrations close <b>5 Jan</b> &lt;script&gt;</p>',
    )
    r = clean.get(f"{URL}/{eid}", headers=a.headers)
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "Example Hack"
    assert body["description"] == "About"
    assert body["needs_review"] is True and body["review_status"] == "needs_review"
    assert body["registration_deadline"] == "2027-01-05T18:29:00Z"
    [src] = body["sources"]
    assert src["url"] == "https://www.devpost.com/h" and src["via"] == "devpost.com"
    assert src["snippet"] == "Registrations close 5 Jan"
    assert "<" not in src["snippet"] and ">" not in src["snippet"]
    assert src["found_at"].endswith("Z")
    listed = clean.get(URL, headers=a.headers, params=ALL).json()["items"][0]
    assert listed["sources"] == body["sources"]
    for key in ("registration_deadline", "event_start"):
        assert listed[key].endswith("Z")
    assert set(body) - set(listed) == {"description", "prize"}
    rejected = event("Rejected", review="rejected")
    for missing in (rejected, str(uuid.uuid4())):
        r = clean.get(f"{URL}/{missing}", headers=a.headers)
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


# --- registration / track: idempotent, own rows only -------------------------------------


@needs_stack
@pytest.mark.parametrize(("path", "flag", "table"), [
    ("registration", "is_registered", "registrations"),
    ("track", "is_tracked", "tracks"),
])  # fmt: skip
def test_interest_is_idempotent_and_per_user(
    clean: TestClient, two_users: Pair, path: str, flag: str, table: str
) -> None:
    a, b = two_users
    eid = event("Shared")
    url = f"{URL}/{eid}/{path}"
    for _ in range(2):
        r = clean.post(url, headers=a.headers)
        assert r.status_code == 200 and r.json() == {flag: True}
    rows = f"SELECT user_id::text FROM {table} WHERE event_id = CAST(:e AS uuid)"
    assert sql(rows, e=eid) == [(a.id,)]

    def flag_for(who: Actor) -> bool:
        return bool(clean.get(f"{URL}/{eid}", headers=who.headers).json()[flag])

    assert flag_for(a) is True
    assert flag_for(b) is False  # A's row never shows as B's flag
    assert clean.get(URL, headers=b.headers, params=ALL).json()["items"][0][flag] is False
    # B's DELETE only ever touches B's own (absent) row.
    assert clean.delete(url, headers=b.headers).json() == {flag: False}
    assert sql(rows, e=eid) == [(a.id,)]
    assert flag_for(a) is True
    for _ in range(2):  # undo, twice
        assert clean.delete(url, headers=a.headers).json() == {flag: False}
    assert sql(rows, e=eid) == []
    for method in ("POST", "DELETE"):
        r = clean.request(method, f"{URL}/{uuid.uuid4()}/{path}", headers=a.headers)
        assert r.status_code == 404
    assert clean.post(url).status_code in (401, 403)  # no session (CSRF may answer first)


@needs_stack
def test_management_and_admin_can_read_the_feed(clean: TestClient) -> None:
    eid = event("Visible")
    for role in ("management", "admin"):
        who = make_actor(f"{role}@example.com", role)
        assert ids(clean, who) == [eid]


# --- performance (HE-050 acceptance) -----------------------------------------------------


@needs_stack
def test_default_list_p95_under_300ms_on_5k_events(clean: TestClient, two_users: Pair) -> None:
    a, _ = two_users
    sql(
        "INSERT INTO organisations (name, kind, region, tier, rank_source, website, is_active)"
        " SELECT 'Org ' || i, CAST(CASE WHEN i % 2 = 0 THEN 'company' ELSE 'institution' END"
        " AS org_kind), CAST(CASE WHEN i % 3 = 0 THEN 'global' ELSE 'india' END AS org_region),"
        " 1 + i % 3, 'test', 'https://x', true FROM generate_series(1, 600) i"
    )
    sql(
        "INSERT INTO events (title, organiser_name, organisation_id, description,"
        " registration_url, registration_deadline, event_start, mode, status, review_status,"
        " confidence, dedupe_key, first_seen_at)"
        " SELECT 'Hack ' || i, 'Org', (SELECT id FROM organisations ORDER BY name"
        "   OFFSET (i % 600) LIMIT 1), 'd', 'https://example.com/' || i,"
        " now() + (i % 120 - 20) * interval '1 day',"  # ~1/6 already past deadline
        " now() + (i % 120) * interval '1 day',"
        " CAST((ARRAY['online','offline','hybrid','unknown'])[1 + i % 4] AS event_mode),"
        " CAST(CASE WHEN i % 10 = 0 THEN 'closed' ELSE 'open' END AS event_status),"
        " 'auto', 0.9, md5(i::text), now() - (i % 30) * interval '1 day'"
        " FROM generate_series(1, 5000) i"
    )
    sql(
        "INSERT INTO event_sources (event_id, source_url, snippet, extracted, extractor)"
        " SELECT id, registration_url, 'Registrations close soon', '{}', 'test' FROM events"
    )
    sql(
        "INSERT INTO registrations (user_id, event_id) SELECT CAST(:u AS uuid), id"
        " FROM events WHERE random() < 0.05",
        u=a.id,
    )
    sql("ANALYZE")
    assert sql("SELECT count(*) FROM events") == [(5000,)]
    ids(clean, a)  # warm-up
    timings = []
    for _ in range(50):
        start = time.perf_counter()
        r = clean.get(URL, headers=a.headers, params=ALL)
        timings.append(time.perf_counter() - start)
        assert r.status_code == 200 and len(r.json()["items"]) == 50
    p95 = statistics.quantiles(timings, n=20)[-1] * 1000
    print(f"GET /events p95 = {p95:.1f} ms over 50 runs on 5k events")
    assert p95 < 300


@needs_stack
def test_default_scope_is_priority_fortune_companies_and_tier1_colleges(
    clean: TestClient, two_users: Pair
) -> None:
    """Owner decision 2026-11-19: the feed shows Fortune-listed companies (any tier) and
    tier 1 colleges only; `scope=all` shows everything."""
    a, _ = two_users
    company3 = event("Company tier 3", org_id=org("Big Co", "company", 3))
    college1 = event("College tier 1", org_id=org("Top College", "institution", 1))
    college2 = event("College tier 2", org_id=org("Mid College", "institution", 2))
    unmatched = event("Unmatched organiser")
    r = clean.get(URL, headers=a.headers)
    assert r.status_code == 200
    assert {e["id"] for e in r.json()["items"]} == {company3, college1}
    assert set(ids(clean, a)) == {company3, college1, college2, unmatched}  # scope=all
    assert clean.get(URL, headers=a.headers, params={"scope": "x"}).status_code == 422
