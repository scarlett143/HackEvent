"""HE-111 weekly digest: Monday 09:00 IST cron, once per ISO week, Management + Admin only."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import redis.asyncio as aioredis
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from tests.stack import DB_URL, REDIS_URL, Actor, make_actor, needs_stack, sql

from common.email import render
from common.queues import NOTIFY_QUEUE
from workers import digest

MON = datetime(2026, 11, 23, 3, 30, tzinfo=UTC)  # Monday 09:00 IST


def test_week_key_follows_the_ist_calendar_week() -> None:
    sunday_late_utc = datetime(2026, 11, 22, 19, 0, tzinfo=UTC)  # Mon 00:30 IST
    assert digest.week_key(sunday_late_utc) == digest.week_key(MON) == "2026-W48"
    assert digest.week_key(datetime(2026, 11, 22, 18, 0, tzinfo=UTC)) == "2026-W47"


def test_cron_fires_monday_0330_utc_whatever_the_host_zone() -> None:
    from workers import settings

    # arq reads settings from the class __dict__ (arq.worker.get_kwargs) and evaluates crons
    # at datetime.now(tz=<timezone setting>); without it, the host's zone.
    kw = vars(settings.SchedulerWorker)
    assert kw["timezone"] is UTC
    (job,) = [j for j in kw["cron_jobs"] if j.name == "cron:weekly_digest"]
    job.calculate_next(datetime(2026, 11, 18, 12, 0, tzinfo=UTC))  # a Wednesday
    assert job.next_run is not None
    assert job.next_run.replace(microsecond=0) == MON


class Queue:
    """Real Redis for the SET NX claim; enqueued jobs are recorded."""

    def __init__(self, r: Any) -> None:
        self.r = r
        self.jobs: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def set(self, *a: Any, **kw: Any) -> Any:
        return await self.r.set(*a, **kw)

    async def enqueue_job(self, name: str, *args: Any, **kw: Any) -> None:
        self.jobs.append((name, args, kw))


def run(now: datetime) -> tuple[int, list[tuple[str, tuple[Any, ...], dict[str, Any]]]]:
    async def go() -> tuple[int, list[Any]]:
        assert DB_URL and REDIS_URL
        r = aioredis.Redis.from_url(REDIS_URL)
        q = Queue(r)
        ctx: dict[str, Any] = {
            "redis": q,
            "db": create_async_engine(DB_URL, poolclass=NullPool),
        }
        try:
            n = await digest.send_weekly_digest(ctx, now=now)
        finally:
            await ctx["db"].dispose()
            await r.aclose()
        return n, q.jobs

    return asyncio.run(go())


@pytest.fixture
def people(stack_client: TestClient) -> dict[str, Actor]:
    sql("TRUNCATE events CASCADE")
    p = {
        "dean": make_actor("dean@example.com", "management"),
        "root": make_actor("root@example.com", "admin"),
        "prof": make_actor("prof@example.com", "professor"),
        "gone": make_actor("gone@example.com", "management"),
    }
    sql("UPDATE users SET is_active = false WHERE id = CAST(:u AS uuid)", u=p["gone"].id)
    sql(
        "UPDATE users SET timezone = 'America/New_York' WHERE id = CAST(:u AS uuid)", u=p["dean"].id
    )
    (ev,) = sql(
        "INSERT INTO events (title, organiser_name, description, registration_url,"
        " registration_deadline, mode, status, review_status, confidence, dedupe_key)"
        " VALUES ('<b>Hack & Co</b>', 'Acme', '', 'https://example.com/r', :d, 'online',"
        " 'open', 'auto', 0.9, :k) RETURNING id::text",
        d=datetime(2026, 11, 25, 12, 0, tzinfo=UTC),
        k=uuid.uuid4().hex,
    )[0]
    sql(
        "INSERT INTO registrations (user_id, event_id) VALUES (CAST(:u AS uuid), CAST(:e AS uuid))",
        u=p["prof"].id,
        e=ev,
    )
    return p


@needs_stack
def test_digest_goes_once_per_week_to_active_management_and_admin(
    people: dict[str, Actor],
) -> None:
    n, jobs = run(MON)
    assert n == 2
    assert [(name, args[0], args[1]) for name, args, _ in jobs] == [
        ("send_email", "dean@example.com", "weekly_digest"),
        ("send_email", "root@example.com", "weekly_digest"),
    ]  # professor and the deactivated manager get nothing
    assert {kw["_queue_name"] for *_, kw in jobs} == {NOTIFY_QUEUE}
    assert jobs[0][2]["_job_id"] == f"digest:2026-W48:{people['dean'].id}"

    # Restart / second scheduler / re-run later the same week: nothing new.
    assert run(MON + timedelta(minutes=1)) == (0, [])
    assert run(MON + timedelta(days=6, hours=12)) == (0, [])  # Sunday evening IST
    n, jobs = run(MON + timedelta(days=7))  # next Monday
    assert n == 2 and jobs[0][2]["_job_id"] == f"digest:2026-W49:{people['dean'].id}"


@needs_stack
def test_digest_email_is_local_time_escaped_and_has_no_addresses(
    people: dict[str, Actor],
) -> None:
    _, jobs = run(MON)
    mails = {args[0]: render(args[1], app_domain="hack.example", **args[2]) for _, args, _ in jobs}
    dean, root = mails["dean@example.com"], mails["root@example.com"]
    assert dean.subject == "HackEvents weekly digest"
    # 25 Nov 12:00 UTC in each recipient's own zone.
    assert "25 Nov 2026, 7:00 AM (America/New_York)" in dean.text
    assert "25 Nov 2026, 5:30 PM (Asia/Kolkata)" in root.text
    assert "Registered: prof" in dean.text and "- prof: 1" in dean.text
    assert "&lt;b&gt;Hack &amp; Co&lt;/b&gt;" in dean.html and "<b>Hack" not in dean.html
    for m in mails.values():
        for p in people.values():
            assert p.email not in m.text and p.email not in m.html
        assert "@" not in m.text and "@" not in m.html
