"""HE-110 dashboard summary: Management + Admin only, college-wide numbers."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from tests.stack import Pair, make_actor, needs_stack, sql

URL = "/api/v1/dashboard/summary"
NOW = datetime.now(UTC)


@pytest.fixture
def c(stack_client: TestClient) -> TestClient:
    sql("TRUNCATE events, organisations CASCADE")
    return stack_client


def event(title: str, deadline: datetime | None, *, review: str = "auto", status: str = "open",
          org: str | None = None, seen: datetime = NOW - timedelta(days=30)) -> str:  # fmt: skip
    return str(
        sql(
            "INSERT INTO events (title, organiser_name, description, registration_url,"
            " registration_deadline, mode, status, review_status, confidence, dedupe_key,"
            " organisation_id, first_seen_at) VALUES (:t, 'Acme', '', 'https://example.com/r',"
            " :d, 'online', CAST(:st AS event_status), CAST(:rv AS review_status), 0.9, :k,"
            " CAST(:o AS uuid), :seen) RETURNING id::text",
            t=title, d=deadline, st=status, rv=review, k=uuid.uuid4().hex, o=org, seen=seen,
        )[0][0]
    )  # fmt: skip


def register(user: str, ev: str) -> None:
    sql(
        "INSERT INTO registrations (user_id, event_id) VALUES (CAST(:u AS uuid), CAST(:e AS uuid))",
        u=user,
        e=ev,
    )


@needs_stack
def test_professor_and_anonymous_are_refused(c: TestClient, two_users: Pair) -> None:
    prof, _ = two_users
    r = c.get(URL, headers=prof.headers)
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    assert c.get(URL).status_code == 401


@needs_stack
def test_management_and_admin_see_college_wide_numbers(c: TestClient, two_users: Pair) -> None:
    a, b = two_users
    mgmt = make_actor("dean@example.com", "management")
    admin = make_actor("root@example.com", "admin")
    (org,) = sql(
        "INSERT INTO organisations (name, kind, region, tier, rank_source, website, is_active)"
        " VALUES ('Acme', 'company', 'india', 1, 'test', 'https://acme.example', true)"
        " RETURNING id::text"
    )[0]
    soon = event("Soon  Hack", NOW + timedelta(days=2), org=org, seen=NOW - timedelta(days=1))
    later = event("Later Hack", NOW + timedelta(days=20))
    event("No Deadline Hack", None)
    past = event("Past Hack", NOW - timedelta(days=1))
    rejected = event("Rejected Hack", NOW + timedelta(days=1), review="rejected")
    event("Cancelled Hack", NOW + timedelta(days=1), status="cancelled")
    register(a.id, soon)
    register(b.id, soon)
    register(a.id, later)
    register(a.id, past)
    register(b.id, rejected)  # invisible event: not counted

    for viewer in (mgmt, admin):
        r = c.get(URL, headers=viewer.headers)
        assert r.status_code == 200, r.text
        s = r.json()
        assert (s["open_events"], s["closing_this_week"], s["new_this_week"]) == (3, 1, 1)
        (closing,) = s["closing_soon"]
        assert closing["id"] == soon and closing["title"] == "Soon Hack"
        assert closing["registered_by"] == ["a", "b"]
        by_user = {u["name"]: u["registrations"] for u in s["registrations_by_user"]}
        assert by_user == {"a": 3, "b": 1, "dean": 0, "root": 0}
        assert s["registrations_by_user"][0]["name"] == "a"
        by_kind = {k["kind"]: k["registrations"] for k in s["registrations_by_kind"]}
        assert by_kind == {"company": 2, "unmatched": 2}
