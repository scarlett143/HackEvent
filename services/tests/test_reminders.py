"""HE-070 reminder planner: timing rules (pure) and the DB contract via the real API."""

import uuid
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from tests.stack import Pair, needs_stack, sql

from common.models import Channel, EventStatus, ReminderKind, ReviewStatus
from common.reminders import Prefs, after_quiet, slot, wanted

IST = ZoneInfo("Asia/Kolkata")


def prefs(**kw: Any) -> Prefs:
    base: dict[str, Any] = {
        "deadline_offsets": [3, 2, 1, 0],
        "event_offsets": [3, 2, 1, 0],
        "send_hour": 9,
        "channels": (Channel.email, Channel.in_app),
        "tz": IST,
        "quiet_start": None,
        "quiet_end": None,
    }
    return Prefs(**(base | kw))


def ist(y: int, m: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=IST)


# --- timing (pure) --------------------------------------------------------------------------


def test_slot_is_send_hour_local_offset_days_before() -> None:
    deadline = ist(2026, 12, 10, 23, 59)
    assert slot(deadline, 3, prefs()) == ist(2026, 12, 7, 9).astimezone(UTC)
    assert slot(deadline, 0, prefs()) == ist(2026, 12, 10, 9).astimezone(UTC)
    assert slot(deadline, 0, prefs()).tzinfo is UTC


def test_slot_uses_the_users_timezone_for_the_day() -> None:
    # 01:00 UTC on 10 Dec is still 9 Dec in New York: day-0 there is 9 Dec 09:00 local.
    deadline = datetime(2026, 12, 10, 1, 0, tzinfo=UTC)
    ny = ZoneInfo("America/New_York")
    assert slot(deadline, 0, prefs(tz=ny)) == datetime(2026, 12, 9, 9, tzinfo=ny).astimezone(UTC)


def test_slot_after_the_anchor_moves_to_an_hour_before_it() -> None:
    deadline = ist(2026, 12, 10, 8, 0)  # closes before the 09:00 send hour
    assert slot(deadline, 0, prefs()) == ist(2026, 12, 10, 7, 0).astimezone(UTC)


@pytest.mark.parametrize(
    ("local", "start", "end", "expected"),
    [
        (ist(2026, 12, 7, 9), None, None, ist(2026, 12, 7, 9)),  # off
        (ist(2026, 12, 7, 9), time(8), time(10), ist(2026, 12, 7, 10)),  # inside
        (ist(2026, 12, 7, 11), time(8), time(10), ist(2026, 12, 7, 11)),  # after
        (ist(2026, 12, 7, 23), time(22), time(7), ist(2026, 12, 8, 7)),  # wraps, evening
        (ist(2026, 12, 7, 6), time(22), time(7), ist(2026, 12, 7, 7)),  # wraps, morning
        (ist(2026, 12, 7, 7), time(22), time(7), ist(2026, 12, 7, 7)),  # end is exclusive
        (ist(2026, 12, 7, 9), time(9), time(9), ist(2026, 12, 7, 9)),  # empty window = off
    ],
)
def test_quiet_hours_push_to_window_end(
    local: datetime, start: time | None, end: time | None, expected: datetime
) -> None:
    assert after_quiet(local, start, end) == expected


class Ev:
    def __init__(self, **kw: Any) -> None:
        self.id = kw.get("id", uuid.uuid4())
        self.status = kw.get("status", EventStatus.open)
        self.review_status = kw.get("review_status", ReviewStatus.auto)
        self.registration_deadline = kw.get("deadline")
        self.event_start = kw.get("start")


NOW = ist(2026, 12, 1, 12)


def test_wanted_registered_uses_event_dates_tracked_uses_deadline() -> None:
    reg = Ev(deadline=ist(2026, 12, 10, 23), start=ist(2026, 12, 20, 10))
    trk = Ev(deadline=ist(2026, 12, 10, 23), start=ist(2026, 12, 20, 10))
    both = Ev(deadline=ist(2026, 12, 10, 23), start=ist(2026, 12, 20, 10))
    events: list[Any] = [(reg, True, False), (trk, False, True), (both, True, True)]
    out = wanted(events, prefs(), NOW)
    assert {(k[0], k[1]) for k in out} == {
        (reg.id, ReminderKind.event),
        (trk.id, ReminderKind.deadline),
        (both.id, ReminderKind.event),  # registered wins: no deadline nagging
    }
    assert len(out) == 3 * 4 * 2  # 3 events x 4 offsets x 2 channels
    assert out[(reg.id, ReminderKind.event, 2, Channel.email)] == ist(2026, 12, 18, 9).astimezone(
        UTC
    )


def test_wanted_skips_past_slots_missing_anchors_and_dead_events() -> None:
    soon = Ev(deadline=ist(2026, 12, 2, 23))  # 1 Dec noon: day-1 = 1 Dec 09:00 is past
    events: list[Any] = [
        (soon, False, True),
        (Ev(deadline=None), False, True),
        (Ev(start=None, deadline=ist(2026, 12, 9)), True, False),  # registered, no start date
        (Ev(deadline=ist(2026, 12, 9), status=EventStatus.cancelled), False, True),
        (Ev(deadline=ist(2026, 12, 9), status=EventStatus.closed), False, True),
        (Ev(deadline=ist(2026, 12, 9), review_status=ReviewStatus.rejected), False, True),
        (Ev(deadline=ist(2026, 11, 30)), False, True),  # deadline passed
    ]
    out = wanted(events, prefs(channels=(Channel.email,)), NOW)
    assert sorted(k[2] for k in out) == [0]  # only soon's day-0 (2 Dec 09:00) is ahead


# --- DB contract via the API --------------------------------------------------------------

# 23:00 IST, 20 days out: every 09:00 slot is before it and in the future.
FUTURE = (datetime.now(IST) + timedelta(days=20)).replace(
    hour=23, minute=0, second=0, microsecond=0
)


def _event(deadline: datetime | None, start: datetime | None = None, status: str = "open") -> str:
    (eid,) = sql(
        "INSERT INTO events (title, organiser_name, description, registration_url,"
        " registration_deadline, event_start, mode, status, review_status, confidence, dedupe_key)"
        " VALUES ('Hack', 'Org', '', 'https://example.com/r', :d, :s, 'online',"
        " CAST(:st AS event_status), 'auto', 0.9, :k) RETURNING id::text",
        d=deadline,
        s=start,
        st=status,
        k=uuid.uuid4().hex,
    )[0]
    return str(eid)


def _rows(user: str) -> list[tuple[Any, ...]]:
    return sql(
        "SELECT event_id::text, kind::text, offset_days, channel::text, status::text, scheduled_at"
        " FROM reminders WHERE user_id = CAST(:u AS uuid)"
        " ORDER BY event_id, kind, offset_days DESC, channel",
        u=user,
    )


@pytest.fixture
def clean(stack_client: TestClient) -> TestClient:
    sql("TRUNCATE events, reminders, notification_prefs CASCADE")
    return stack_client


@needs_stack
def test_track_plans_deadline_reminders_untrack_removes_them(
    clean: TestClient, two_users: Pair
) -> None:
    a, b = two_users
    eid = _event(FUTURE, FUTURE + timedelta(days=10))
    assert clean.post(f"/api/v1/events/{eid}/track", headers=a.headers).status_code == 200
    rows = _rows(a.id)
    assert {(r[1], r[2], r[3]) for r in rows} == {
        ("deadline", d, c) for d in (3, 2, 1, 0) for c in ("email", "in_app")
    }
    assert all(r[4] == "pending" for r in rows)
    assert _rows(b.id) == []  # A's action plans nothing for B
    assert clean.delete(f"/api/v1/events/{eid}/track", headers=a.headers).status_code == 200
    assert _rows(a.id) == []


@needs_stack
def test_register_switches_to_event_reminders(clean: TestClient, two_users: Pair) -> None:
    a, _ = two_users
    eid = _event(FUTURE, FUTURE + timedelta(days=10))
    clean.post(f"/api/v1/events/{eid}/track", headers=a.headers)
    clean.post(f"/api/v1/events/{eid}/registration", headers=a.headers)
    assert {r[1] for r in _rows(a.id)} == {"event"}
    clean.delete(f"/api/v1/events/{eid}/registration", headers=a.headers)
    assert {r[1] for r in _rows(a.id)} == {"deadline"}  # still tracked


@needs_stack
def test_replanning_is_idempotent_and_never_rearms_a_sent_reminder(
    clean: TestClient, two_users: Pair
) -> None:
    a, _ = two_users
    eid = _event(FUTURE)
    clean.post(f"/api/v1/events/{eid}/track", headers=a.headers)
    before = _rows(a.id)
    sql(
        "UPDATE reminders SET status = 'sent', sent_at = now() WHERE user_id = CAST(:u AS uuid)"
        " AND offset_days = 3 AND channel = 'email'",
        u=a.id,
    )
    clean.post(f"/api/v1/events/{eid}/track", headers=a.headers)  # replan
    # a timezone change moves every pending slot; the sent one must stay as it was
    r = clean.patch("/api/v1/me", json={"timezone": "America/New_York"}, headers=a.headers)
    assert r.status_code == 200, r.text
    after = _rows(a.id)
    assert len(after) == len(before)  # no duplicates, ever
    sent = [x for x in after if x[4] == "sent"]
    assert [(x[2], x[3]) for x in sent] == [(3, "email")]
    old = {(x[2], x[3]): x[5] for x in before}
    assert sent[0][5] == old[(3, "email")]  # a sent row is never rescheduled
    moved = next(x for x in after if x[2] == 2 and x[3] == "email")
    assert moved[4] == "pending" and moved[5] != old[(2, "email")]


@needs_stack
def test_unique_key_rejects_a_second_reminder(clean: TestClient, two_users: Pair) -> None:
    a, _ = two_users
    eid = _event(FUTURE)
    clean.post(f"/api/v1/events/{eid}/track", headers=a.headers)
    with pytest.raises(Exception, match="(?i)unique|duplicate"):
        sql(
            "INSERT INTO reminders (user_id, event_id, kind, offset_days, channel, scheduled_at,"
            " status) VALUES (CAST(:u AS uuid), CAST(:e AS uuid), 'deadline', 0, 'in_app', now(),"
            " 'pending')",
            u=a.id,
            e=eid,
        )


@needs_stack
def test_prefs_change_replans_offsets_and_channels(clean: TestClient, two_users: Pair) -> None:
    a, _ = two_users
    eid = _event(FUTURE)
    clean.post(f"/api/v1/events/{eid}/track", headers=a.headers)
    body = {
        "deadline_offsets_days": [7, 1],
        "event_offsets_days": [1],
        "channels": {"email": False, "in_app": True},
    }
    r = clean.put("/api/v1/me/notification-prefs", json=body, headers=a.headers)
    assert r.status_code == 200, r.text
    assert {(r[2], r[3]) for r in _rows(a.id)} == {(7, "in_app"), (1, "in_app")}


@needs_stack
def test_quiet_hours_move_reminders_out_of_the_window(clean: TestClient, two_users: Pair) -> None:
    a, _ = two_users
    eid = _event(FUTURE)
    clean.post(f"/api/v1/events/{eid}/track", headers=a.headers)
    r = clean.patch(
        "/api/v1/me", json={"quiet_start": "08:00", "quiet_end": "10:30"}, headers=a.headers
    )
    assert r.status_code == 200, r.text
    rows = _rows(a.id)
    assert rows
    for row in rows:
        local = row[5].astimezone(IST)
        assert (local.hour, local.minute) == (10, 30), row


@needs_stack
def test_deactivated_user_has_no_pending_reminders(clean: TestClient, two_users: Pair) -> None:
    from tests.stack import make_actor

    a, _ = two_users
    admin = make_actor(f"admin-{uuid.uuid4().hex[:8]}@example.org", "admin")
    eid = _event(FUTURE)
    clean.post(f"/api/v1/events/{eid}/track", headers=a.headers)
    assert _rows(a.id)
    r = clean.patch(f"/api/v1/admin/users/{a.id}", json={"is_active": False}, headers=admin.headers)
    assert r.status_code == 200, r.text
    assert _rows(a.id) == []
    r = clean.patch(f"/api/v1/admin/users/{a.id}", json={"is_active": True}, headers=admin.headers)
    assert r.status_code == 200, r.text
    assert len(_rows(a.id)) == 8  # reactivated: planned again from the kept track
