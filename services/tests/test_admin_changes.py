"""Admin "Changes" list and manual "Refresh now" (api/admin_changes.py)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import redis
from fastapi.testclient import TestClient
from tests.stack import REDIS_URL, Actor, make_actor, needs_stack, sql

from api.admin_changes import REFRESH_KEY

NOW = datetime.now(UTC)


@pytest.fixture
def c(stack_client: TestClient) -> TestClient:
    sql("TRUNCATE events, sources, organisations CASCADE")
    assert REDIS_URL
    redis.Redis.from_url(REDIS_URL).delete(REFRESH_KEY)
    return stack_client


@pytest.fixture
def admin(c: TestClient) -> Actor:
    return make_actor(f"admin-{uuid.uuid4().hex[:6]}@example.com", "admin")


def event(title: str, organiser: str = "Somebody", *, first_seen: datetime = NOW,
          deadline_changed: datetime | None = None, status: str = "open",
          review: str = "auto") -> str:  # fmt: skip
    return str(
        sql(
            "INSERT INTO events (title, organiser_name, description, registration_url, mode,"
            " status, review_status, confidence, dedupe_key, first_seen_at, deadline_changed_at)"
            " VALUES (:t, :o, '', 'https://example.com/r', 'online', CAST(:s AS event_status),"
            " CAST(:rv AS review_status), 0.9, :k, :f, :dc) RETURNING id::text",
            t=title, o=organiser, s=status, rv=review, k=uuid.uuid4().hex, f=first_seen,
            dc=deadline_changed,
        )[0][0]
    )  # fmt: skip


@needs_stack
def test_changes_lists_new_moved_and_cancelled_with_priority_flag(
    c: TestClient, admin: Actor
) -> None:
    (org,) = sql(
        "INSERT INTO organisations (name, kind, region, tier, rank_source, website, is_active)"
        " VALUES ('Infosys', 'company', 'india', 1, 't', 'https://x', true) RETURNING id::text"
    )[0]
    fresh = event("<b>Fresh</b> Hack")
    sql("UPDATE events SET organisation_id = CAST(:o AS uuid)", o=org)
    moved = event("Moved", first_seen=NOW - timedelta(days=5), deadline_changed=NOW)
    cancelled = event("Off", first_seen=NOW - timedelta(days=5), status="cancelled")
    event("Old", first_seen=NOW - timedelta(days=5))  # nothing in the window
    event("Spam", review="rejected")
    r = c.get("/api/v1/admin/changes", headers=admin.headers)
    assert r.status_code == 200, r.text
    got = {(i["event_id"], i["change"], i["priority"]) for i in r.json()["items"]}
    assert got == {(fresh, "new", True), (moved, "deadline_changed", False),
                   (cancelled, "cancelled", False)}  # fmt: skip
    titles = {i["title"] for i in r.json()["items"]}
    assert "Fresh Hack" in titles  # plain text
    assert r.json()["crawl"]["refresh_available_at"] is None
    assert (
        c.get("/api/v1/admin/changes", params={"hours": 0}, headers=admin.headers).status_code
        == 422
    )


@needs_stack
def test_refresh_queues_sources_links_events_and_has_a_cooldown(
    c: TestClient, admin: Actor
) -> None:
    sql(
        "INSERT INTO organisations (name, kind, region, tier, rank_source, website, is_active)"
        " VALUES ('Indian Institute of Technology Bombay', 'institution', 'india', 1, 't',"
        " 'https://x', true)"
    )
    for url, enabled in (("https://a.example", True), ("https://b.example", False)):
        sql(
            "INSERT INTO sources (kind, url, adapter, cadence_hours, is_enabled, next_run_at)"
            " VALUES ('aggregator', :u, 'llm', 1, :e, now() + interval '1 hour')",
            u=url, e=enabled,
        )  # fmt: skip
    ev = event("Techfest", "IIT Bombay")
    r = c.post("/api/v1/admin/refresh", headers=admin.headers)
    assert r.status_code == 202, r.text
    assert r.json()["sources_queued"] == 1 and r.json()["events_linked"] == 1
    due = sql("SELECT url FROM sources WHERE next_run_at <= now()")
    assert [d[0] for d in due] == ["https://a.example"]  # disabled source untouched
    assert sql("SELECT organisation_id IS NOT NULL FROM events WHERE id = CAST(:e AS uuid)",
               e=ev) == [(True,)]  # fmt: skip
    again = c.post("/api/v1/admin/refresh", headers=admin.headers)
    assert again.status_code == 429 and int(again.headers["Retry-After"]) > 0
    status = c.get("/api/v1/admin/changes", headers=admin.headers).json()["crawl"]
    assert status["refresh_available_at"] is not None and status["sources_enabled"] == 1


@needs_stack
def test_professors_cannot_see_or_refresh(c: TestClient) -> None:
    prof = make_actor("prof@example.com")
    assert c.get("/api/v1/admin/changes", headers=prof.headers).status_code == 403
    assert c.post("/api/v1/admin/refresh", headers=prof.headers).status_code == 403
