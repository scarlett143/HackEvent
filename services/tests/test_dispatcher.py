"""HE-071 reminder dispatcher: due sweep, at-most-once claim, channels, retries."""

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from tests.stack import DB_URL, make_actor, needs_stack, sql

from common.email import EmailError, RenderedEmail, TokenRevoked
from common.queues import NOTIFY_QUEUE
from workers import reminders

NOW = datetime(2026, 12, 1, 6, 0, tzinfo=UTC)  # 11:30 IST


class FakeQueue:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def enqueue_job(self, name: str, *args: Any, **kw: Any) -> None:
        self.jobs.append((name, args, kw))


class FakeGmail:
    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[tuple[str, RenderedEmail]] = []
        self.error = error

    async def send(self, to: str, email: RenderedEmail) -> str:
        if self.error:
            raise self.error
        self.sent.append((to, email))
        return "msg-1"


# --- wording (pure) -------------------------------------------------------------------------


def _row(**kw: Any) -> Any:
    base = {
        "timezone": "Asia/Kolkata",
        "kind": "deadline",
        "title": "Acme\\nCodeSprint",
        "event_id": "e1",
        "registration_deadline": datetime(2026, 12, 3, 18, 29, tzinfo=UTC),  # 3 Dec 23:59 IST
        "event_start": datetime(2026, 12, 1, 4, 30, tzinfo=UTC),  # 1 Dec 10:00 IST
    }
    return SimpleNamespace(**(base | kw))


def test_message_wording() -> None:
    m = reminders.message(_row(), NOW)
    assert m["headline"] == "2 days left to register"
    assert m["when"] == "3 Dec 2026, 11:59 PM (Asia/Kolkata)"
    assert m["title"] == "Acme\\nCodeSprint" and m["path"] == "/events/e1"
    assert (
        reminders.message(_row(registration_deadline=NOW + timedelta(hours=2)), NOW)["headline"]
        == "Last day to register"
    )
    ev = reminders.message(_row(kind="event"), NOW)
    assert ev["headline"] == "Starts today" and ev["when_label"] == "Starts"
    assert ev["when"] == "1 Dec 2026, 10:00 AM (Asia/Kolkata)"


# --- DB -------------------------------------------------------------------------------------


@pytest.fixture
def engine() -> Iterator[AsyncEngine]:
    assert DB_URL
    sql("TRUNCATE events, reminders, notifications CASCADE")
    eng = create_async_engine(DB_URL, poolclass=NullPool)  # one asyncio.run per call
    yield eng
    asyncio.run(eng.dispose())


def _event(deadline: datetime | None, start: datetime | None = None, status: str = "open") -> str:
    return str(
        sql(
            "INSERT INTO events (title, organiser_name, description, registration_url,"
            " registration_deadline, event_start, mode, status, review_status, confidence,"
            " dedupe_key) VALUES ('Acme CodeSprint', 'Acme', '', 'https://example.com/r', :d,"
            " :s, 'online', CAST(:st AS event_status), 'auto', 0.9, :k) RETURNING id::text",
            d=deadline,
            s=start,
            st=status,
            k=uuid.uuid4().hex,
        )[0][0]
    )


def _reminder(user: str, event: str, *, channel: str = "in_app", at: datetime = NOW,
              kind: str = "deadline", offset: int = 2) -> str:  # fmt: skip
    return str(
        sql(
            "INSERT INTO reminders (user_id, event_id, kind, offset_days, channel, scheduled_at,"
            " status) VALUES (CAST(:u AS uuid), CAST(:e AS uuid), CAST(:k AS reminder_kind), :o,"
            " CAST(:c AS channel), :at, 'pending') RETURNING id::text",
            u=user, e=event, k=kind, o=offset, c=channel, at=at,
        )[0][0]
    )  # fmt: skip


def _state(rid: str) -> tuple[Any, ...]:
    return tuple(
        sql(
            "SELECT status::text, attempts, sent_at IS NOT NULL, scheduled_at, last_error"
            " FROM reminders WHERE id = CAST(:i AS uuid)",
            i=rid,
        )[0]
    )


def _user() -> str:
    return make_actor(f"u-{uuid.uuid4().hex[:8]}@example.org").id


DEADLINE = NOW + timedelta(days=2, hours=12)


@needs_stack
def test_dispatch_enqueues_only_due_pending_with_attempt_scoped_ids(engine: AsyncEngine) -> None:
    u, e = _user(), _event(DEADLINE)
    due = _reminder(u, e, at=NOW - timedelta(minutes=1))
    _reminder(u, e, at=NOW + timedelta(minutes=1), offset=1)  # not yet
    done = _reminder(u, e, at=NOW - timedelta(hours=1), offset=3)
    sql("UPDATE reminders SET status = 'sent' WHERE id = CAST(:i AS uuid)", i=done)
    q = FakeQueue()
    assert asyncio.run(reminders.dispatch_due({"redis": q, "db": engine}, now=NOW)) == 1
    ((name, args, kw),) = q.jobs
    assert name == "send_reminder" and args == (due,)
    assert kw == {"_job_id": f"reminder:{due}:0", "_queue_name": NOTIFY_QUEUE}


@needs_stack
def test_in_app_is_delivered_once_even_with_concurrent_jobs(engine: AsyncEngine) -> None:
    u, e = _user(), _event(DEADLINE)
    rid = _reminder(u, e)
    ctx = {"db": engine}

    async def race() -> list[str]:
        return list(
            await asyncio.gather(*(reminders.send_reminder(ctx, rid, now=NOW) for _ in range(5)))
        )

    outcomes = asyncio.run(race())
    assert sorted(outcomes) == ["sent", "skipped", "skipped", "skipped", "skipped"]
    assert _state(rid)[:3] == ("sent", 1, True)
    notes = sql(
        "SELECT type, title, body, event_id::text FROM notifications WHERE user_id = CAST(:u AS uuid)",
        u=u,
    )
    assert notes == [
        ("reminder", "2 days left to register: Acme CodeSprint",
         "Registration closes: 3 Dec 2026, 11:30 PM (Asia/Kolkata)", e)
    ]  # fmt: skip
    assert asyncio.run(reminders.send_reminder(ctx, rid, now=NOW)) == "skipped"


@needs_stack
def test_email_sent_via_gmail(engine: AsyncEngine) -> None:
    u, e = _user(), _event(DEADLINE)
    rid = _reminder(u, e, channel="email")
    gmail = FakeGmail()
    assert (
        asyncio.run(reminders.send_reminder({"db": engine, "gmail": gmail}, rid, now=NOW)) == "sent"
    )
    ((to, email),) = gmail.sent
    assert (
        to.endswith("@example.org") and email.subject == "2 days left to register: Acme CodeSprint"
    )
    assert f"/events/{e}" in email.text
    assert _state(rid)[:3] == ("sent", 1, True)


@needs_stack
def test_email_failure_retries_with_backoff_then_fails(engine: AsyncEngine) -> None:
    u, e = _user(), _event(DEADLINE)
    rid = _reminder(u, e, channel="email")
    ctx = {"db": engine, "gmail": FakeGmail(EmailError("Gmail send failed: HTTP 503"))}
    assert asyncio.run(reminders.send_reminder(ctx, rid, now=NOW)) == "retry"
    status, attempts, sent, at, err = _state(rid)
    assert (status, attempts, sent) == ("pending", 1, False)
    assert at == NOW + timedelta(minutes=5) and "HTTP 503" in err
    sql("UPDATE reminders SET attempts = :n, scheduled_at = :t WHERE id = CAST(:i AS uuid)",
        n=reminders.MAX_ATTEMPTS - 1, t=NOW, i=rid)  # fmt: skip
    assert asyncio.run(reminders.send_reminder(ctx, rid, now=NOW)) == "failed"
    assert _state(rid)[:3] == ("failed", reminders.MAX_ATTEMPTS, False)


@needs_stack
def test_revoked_gmail_token_fails_at_once(engine: AsyncEngine) -> None:
    u, e = _user(), _event(DEADLINE)
    rid = _reminder(u, e, channel="email")
    ctx = {"db": engine, "gmail": FakeGmail(TokenRevoked("invalid_grant"))}
    assert asyncio.run(reminders.send_reminder(ctx, rid, now=NOW)) == "failed"
    assert _state(rid)[0] == "failed"


@needs_stack
def test_channel_without_sender_fails_with_reason(engine: AsyncEngine) -> None:
    u, e = _user(), _event(DEADLINE)
    rid = _reminder(u, e, channel="telegram")
    assert asyncio.run(reminders.send_reminder({"db": engine}, rid, now=NOW)) == "failed"
    assert _state(rid)[4] == "no sender for channel telegram yet"


@needs_stack
@pytest.mark.parametrize(
    "setup",
    ["cancelled", "closed", "deadline_passed", "inactive_user"],
)
def test_reminders_that_no_longer_apply_are_cancelled(engine: AsyncEngine, setup: str) -> None:
    u = _user()
    e = _event(
        NOW - timedelta(minutes=5) if setup == "deadline_passed" else DEADLINE,
        status=setup if setup in ("cancelled", "closed") else "open",
    )
    if setup == "inactive_user":
        sql("UPDATE users SET is_active = false WHERE id = CAST(:u AS uuid)", u=u)
    rid = _reminder(u, e, at=NOW - timedelta(minutes=10))
    assert asyncio.run(reminders.send_reminder({"db": engine}, rid, now=NOW)) == "cancelled"
    assert _state(rid)[:3] == ("cancelled", 1, False)
    assert sql("SELECT count(*) FROM notifications")[0][0] == 0


@needs_stack
def test_not_yet_due_is_skipped(engine: AsyncEngine) -> None:
    u, e = _user(), _event(DEADLINE)
    rid = _reminder(u, e, at=NOW + timedelta(minutes=1))
    assert asyncio.run(reminders.send_reminder({"db": engine}, rid, now=NOW)) == "skipped"
    assert _state(rid)[:2] == ("pending", 0)
