"""HE-093 admin review queue: list, approve / edit + approve, reject, merge."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.stack import Actor, Pair, make_actor, needs_stack, sql

URL = "/api/v1/admin/review"
SOON = (datetime.now(UTC) + timedelta(days=20)).replace(hour=17, minute=0, second=0, microsecond=0)


@pytest.fixture
def c(stack_client: TestClient) -> TestClient:
    sql("TRUNCATE events, reminders, notifications, audit_log CASCADE")
    return stack_client


@pytest.fixture
def admin(c: TestClient) -> Actor:
    return make_actor(f"admin-{uuid.uuid4().hex[:6]}@example.com", "admin")


def event(title: str, review: str = "needs_review", deadline: datetime | None = SOON) -> str:
    return str(
        sql(
            "INSERT INTO events (title, organiser_name, description, registration_url,"
            " registration_deadline, event_start, mode, status, review_status, confidence,"
            " dedupe_key) VALUES (:t, 'Example Org', '', 'https://example.com/r', :d, :s,"
            " 'online', 'open', CAST(:rv AS review_status), 0.6, :k) RETURNING id::text",
            t=title, d=deadline, s=SOON + timedelta(days=10), rv=review, k=uuid.uuid4().hex,
        )[0][0]
    )  # fmt: skip


def source(eid: str, url: str, reasons: list[str] | None = None) -> None:
    import json

    sql(
        "INSERT INTO event_sources (event_id, source_url, snippet, extracted, extractor)"
        " VALUES (CAST(:e AS uuid), :u, '<b>Registrations close</b> 20 Dec', CAST(:x AS jsonb),"
        " 'gemini:flash')",
        e=eid, u=url, x=json.dumps({"review_reasons": reasons or []}),
    )  # fmt: skip


def reminders_of(user: str) -> list[tuple[Any, ...]]:
    return sql(
        "SELECT event_id::text, status::text, scheduled_at FROM reminders"
        " WHERE user_id = CAST(:u AS uuid) ORDER BY scheduled_at",
        u=user,
    )


@needs_stack
def test_queue_is_admin_only(c: TestClient, two_users: Pair) -> None:
    a, _ = two_users
    assert c.get(URL, headers=a.headers).status_code == 403
    assert (
        c.post(f"{URL}/{uuid.uuid4()}", json={"action": "reject"}, headers=a.headers).status_code
        == 403
    )


@needs_stack
def test_queue_lists_needs_review_with_sources_reasons_and_duplicates(
    c: TestClient, admin: Actor
) -> None:
    pending = event("CodeSprint Hackathon 2027")
    source(pending, "https://a.example/cs", ["deadline_unsupported"])
    twin = event("CodeSprint Hackathon", review="auto")
    event("Unrelated Robotics Meetup", review="auto")
    event("Rejected One", review="rejected")
    r = c.get(URL, headers=admin.headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    (item,) = body["items"]
    assert item["id"] == pending
    (src,) = item["sources"]
    assert src["snippet"] == "Registrations close 20 Dec"  # plain text, tags stripped
    assert src["reasons"] == ["deadline_unsupported"]
    assert [d["id"] for d in item["duplicates"]] == [twin]


@needs_stack
def test_edit_and_approve_replans_reminders_and_audits(
    c: TestClient, admin: Actor, two_users: Pair
) -> None:
    a, _ = two_users
    eid = event("Hack")
    assert c.post(f"/api/v1/events/{eid}/track", headers=a.headers).status_code == 200
    before = reminders_of(a.id)
    assert before
    moved = SOON + timedelta(days=3)
    patch = {"title": "Hack 2027", "registration_deadline": moved.isoformat()}
    r = c.post(f"{URL}/{eid}", json={"action": "approve", "patch": patch}, headers=admin.headers)
    assert r.status_code == 200, r.text
    assert r.json()["review_status"] == "approved"
    row = sql(
        "SELECT title, registration_deadline, deadline_changed_at IS NOT NULL, review_status::text"
        " FROM events WHERE id = CAST(:e AS uuid)",
        e=eid,
    )[0]
    assert row == ("Hack 2027", moved, True, "approved")
    after = reminders_of(a.id)
    assert len(after) == len(before)
    assert all(x[2] == y[2] + timedelta(days=3) for x, y in zip(after, before, strict=True))
    (audit,) = sql("SELECT action, meta FROM audit_log WHERE action = 'review.decision'")
    assert audit[1] == {"action": "approve", "fields": ["registration_deadline", "title"]}
    again = c.post(f"{URL}/{eid}", json={"action": "reject"}, headers=admin.headers)
    assert again.status_code == 409


@needs_stack
def test_reject_hides_event_and_drops_pending_reminders(
    c: TestClient, admin: Actor, two_users: Pair
) -> None:
    a, _ = two_users
    eid = event("Spammy Hack")
    c.post(f"/api/v1/events/{eid}/track", headers=a.headers)
    assert reminders_of(a.id)
    assert (
        c.post(f"{URL}/{eid}", json={"action": "reject"}, headers=admin.headers).status_code == 200
    )
    assert reminders_of(a.id) == []
    assert c.get(f"/api/v1/events/{eid}", headers=a.headers).status_code == 404
    assert eid not in c.get("/api/v1/events", headers=a.headers).text


@needs_stack
def test_merge_moves_people_and_sources_then_deletes_duplicate(
    c: TestClient, admin: Actor, two_users: Pair
) -> None:
    a, b = two_users
    target = event("CodeSprint", review="auto")
    dup = event("CodeSprint 2027")
    source(target, "https://a.example/cs")
    source(dup, "https://a.example/cs")  # same URL as target's: dropped, not duplicated
    source(dup, "https://b.example/cs")
    c.post(f"/api/v1/events/{dup}/registration", headers=a.headers)
    c.post(f"/api/v1/events/{dup}/track", headers=b.headers)
    c.post(f"/api/v1/events/{target}/track", headers=b.headers)  # B already tracks target
    r = c.post(
        f"{URL}/{dup}", json={"action": "merge", "merge_into": target}, headers=admin.headers
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"id": dup, "action": "merge", "review_status": None}
    assert sql("SELECT count(*) FROM events WHERE id = CAST(:e AS uuid)", e=dup) == [(0,)]
    urls = sql(
        "SELECT source_url FROM event_sources WHERE event_id = CAST(:e AS uuid) ORDER BY 1",
        e=target,
    )
    assert urls == [("https://a.example/cs",), ("https://b.example/cs",)]
    assert sql(
        "SELECT user_id::text FROM registrations WHERE event_id = CAST(:e AS uuid)", e=target
    ) == [(a.id,)]
    assert sql("SELECT count(*) FROM tracks WHERE event_id = CAST(:e AS uuid)", e=target) == [(1,)]
    assert {x[0] for x in reminders_of(a.id)} == {target}  # replanned on the target
    assert {x[0] for x in reminders_of(b.id)} == {target}


@needs_stack
@pytest.mark.parametrize(
    "body",
    [
        {"action": "merge"},
        {"action": "approve", "merge_into": str(uuid.uuid4())},
        {"action": "reject", "patch": {"title": "x"}},
        {"action": "approve", "patch": {"registration_deadline": "2027-01-01T10:00:00"}},
        {"action": "approve", "patch": {"registration_url": "javascript:alert(1)"}},
        {"action": "delete"},
    ],
)
def test_bad_decisions_are_422(c: TestClient, admin: Actor, body: dict[str, Any]) -> None:
    eid = event("Hack")
    assert c.post(f"{URL}/{eid}", json=body, headers=admin.headers).status_code == 422


@needs_stack
def test_merge_target_must_be_another_live_event(c: TestClient, admin: Actor) -> None:
    eid = event("Hack")
    gone = event("Gone", review="rejected")
    for target in (eid, gone, str(uuid.uuid4())):
        r = c.post(
            f"{URL}/{eid}", json={"action": "merge", "merge_into": target}, headers=admin.headers
        )
        assert r.status_code == 422, target
    assert (
        c.post(
            f"{URL}/{uuid.uuid4()}", json={"action": "reject"}, headers=admin.headers
        ).status_code
        == 404
    )
