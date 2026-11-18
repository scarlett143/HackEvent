"""HE-078 new-event alerts: matching users only, in-app + email by channel prefs, once."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import redis.asyncio as aioredis
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from tests.stack import DB_URL, REDIS_URL, Actor, make_actor, needs_stack, sql

from common import new_event_alerts
from common.email import render
from common.new_event_alerts import matches
from common.queues import NOTIFY_QUEUE

DEADLINE = datetime.now(UTC).replace(microsecond=0) + timedelta(days=10)


def test_matches_like_the_feed_filters() -> None:
    ev: dict[str, Any] = {"region": "india", "kind": None, "mode": "online"}
    assert matches(None, **ev) and matches({}, **ev)
    assert matches({"region": [], "kind": [], "mode": []}, **ev)  # empty = any
    assert matches({"region": ["india"], "mode": ["online", "hybrid"]}, **ev)
    assert matches({"region": "india"}, **ev)  # a bare string counts as one value
    assert not matches({"region": ["global"]}, **ev)
    assert not matches({"mode": ["offline"]}, **ev)
    assert not matches({"kind": ["company"]}, **ev)  # no matched organisation, no kind
    assert matches({"kind": ["company"]}, region="global", kind="company", mode="unknown")
    assert not matches({"mode": ["online"]}, region="global", kind="company", mode="unknown")


class Queue:
    """Real Redis for the email claim; enqueued jobs are recorded."""

    def __init__(self, r: Any) -> None:
        self.r = r
        self.jobs: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def set(self, *a: Any, **kw: Any) -> Any:
        return await self.r.set(*a, **kw)

    async def incr(self, *a: Any) -> Any:
        return await self.r.incr(*a)

    async def expire(self, *a: Any) -> Any:
        return await self.r.expire(*a)

    async def enqueue_job(self, name: str, *args: Any, **kw: Any) -> None:
        self.jobs.append((name, args, kw))


def notify(event_id: str) -> list[tuple[str, tuple[Any, ...], dict[str, Any]]]:
    async def go() -> list[Any]:
        assert DB_URL and REDIS_URL
        engine = create_async_engine(DB_URL, poolclass=NullPool)
        r = aioredis.Redis.from_url(REDIS_URL)
        q = Queue(r)
        try:
            await new_event_alerts.notify(engine, q, uuid.UUID(event_id))
        finally:
            await engine.dispose()
            await r.aclose()
        return q.jobs

    return asyncio.run(go())


def event(*, location: str | None = "Bengaluru, India", review: str = "auto",
          deadline: datetime | None = DEADLINE, status: str = "open",
          title: str = "Acme <b>CodeSprint</b>") -> str:  # fmt: skip
    return str(
        sql(
            "INSERT INTO events (title, organiser_name, description, registration_url,"
            " registration_deadline, mode, location, status, review_status, confidence,"
            " dedupe_key) VALUES (:t, 'Acme', '', 'https://example.com/r', :d, 'online', :l,"
            " CAST(:st AS event_status), CAST(:rv AS review_status), 0.9, :k) RETURNING id::text",
            t=title, d=deadline, l=location, st=status, rv=review, k=uuid.uuid4().hex,
        )[0][0]
    )  # fmt: skip


def prefs(user: Actor, *, alerts: bool = True, filters: dict[str, Any] | None = None,
          channels: dict[str, bool] | None = None) -> None:  # fmt: skip
    sql(
        "INSERT INTO notification_prefs (user_id, new_event_alerts, new_event_filters, channels)"
        " VALUES (CAST(:u AS uuid), :a, CAST(:f AS jsonb), CAST(:c AS jsonb))",
        u=user.id, a=alerts, f=json.dumps(filters or {}), c=json.dumps(channels or {}),
    )  # fmt: skip


def inbox(event_id: str) -> list[tuple[str, str, str]]:
    return [
        (r[0], r[1], r[2])
        for r in sql(
            "SELECT u.email::text, n.title, n.body FROM notifications n JOIN users u"
            " ON u.id = n.user_id WHERE n.event_id = CAST(:e AS uuid) AND n.type = 'new_event'"
            " ORDER BY u.email",
            e=event_id,
        )
    ]


@pytest.fixture
def users(stack_client: TestClient) -> dict[str, Actor]:
    sql("TRUNCATE events, notifications CASCADE")
    names = [
        "plain",
        "india",
        "global",
        "company",
        "offline",
        "off",
        "mailonly",
        "inboxonly",
        "gone",
    ]
    u = {n: make_actor(f"{n}@example.com") for n in names}
    prefs(u["india"], filters={"region": ["india"], "mode": ["online"]})
    prefs(u["global"], filters={"region": ["global"]})
    prefs(u["company"], filters={"kind": ["company"]})
    prefs(u["offline"], filters={"mode": ["offline"]})
    prefs(u["off"], alerts=False)
    prefs(u["mailonly"], channels={"in_app": False, "email": True})
    prefs(u["inboxonly"], channels={"email": False})
    sql("UPDATE users SET is_active = false WHERE id = CAST(:u AS uuid)", u=u["gone"].id)
    sql("UPDATE users SET timezone = 'Europe/London' WHERE id = CAST(:u AS uuid)", u=u["plain"].id)
    return u


@needs_stack
def test_only_matching_users_are_alerted_once_by_their_channels(users: dict[str, Actor]) -> None:
    ev = event()
    jobs = notify(ev)
    assert [e for e, *_ in inbox(ev)] == [
        "inboxonly@example.com",
        "india@example.com",
        "plain@example.com",
    ]
    assert [(n, a[0], a[1]) for n, a, _ in jobs] == [
        ("send_email", "india@example.com", "new_event"),
        ("send_email", "mailonly@example.com", "new_event"),
        ("send_email", "plain@example.com", "new_event"),
    ]
    assert {kw["_queue_name"] for *_, kw in jobs} == {NOTIFY_QUEUE}
    assert jobs[0][2]["_job_id"] == f"new_event:{ev}:{users['india'].id}"

    # Dates in each user's own timezone; the title stays plain text.
    by_to = {a[0]: a[2] for _, a, _ in jobs}
    london = DEADLINE.astimezone(ZoneInfo("Europe/London"))
    assert by_to["plain@example.com"]["when"].endswith("(Europe/London)")
    assert by_to["plain@example.com"]["when"].startswith(f"{london.day} {london:%b %Y}")
    assert by_to["india@example.com"]["when"].endswith("(Asia/Kolkata)")
    mail = render("new_event", app_domain="hack.example", **by_to["plain@example.com"])
    assert mail.subject == "New hackathon: Acme <b>CodeSprint</b>"
    assert "Acme &lt;b&gt;CodeSprint&lt;/b&gt;" in mail.html and "<b>CodeSprint" not in mail.html
    assert f"https://hack.example/events/{ev}" in mail.text

    # A repeated call (retry, second trigger) sends nothing new on any channel.
    assert notify(ev) == []
    assert len(inbox(ev)) == 3


@needs_stack
def test_region_and_trust_rules(users: dict[str, Actor]) -> None:
    abroad = event(location=None)  # no organisation, no India location: "global"
    notify(abroad)
    assert [e for e, *_ in inbox(abroad)] == [
        "global@example.com",
        "inboxonly@example.com",
        "plain@example.com",
    ]
    for skipped in (
        event(review="needs_review"),
        event(review="rejected"),
        event(deadline=datetime.now(UTC) - timedelta(hours=1)),
        event(status="cancelled"),
    ):
        assert notify(skipped) == [] and inbox(skipped) == []


@needs_stack
def test_first_crawl_flood_is_capped_per_user_per_day(users: dict[str, Actor]) -> None:
    """Many events created at once: each user gets at most EMAIL_DAILY_CAP emails a day,
    every event still reaches the inbox."""
    cap = new_event_alerts.EMAIL_DAILY_CAP
    events = [event(title=f"Flood {i}") for i in range(cap + 3)]
    mails = [job for e in events for job in notify(e) if job[1][0] == "plain@example.com"]
    assert len(mails) == cap
    for e in events:
        assert "plain@example.com" in [row[0] for row in inbox(e)]
