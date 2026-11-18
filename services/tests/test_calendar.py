"""HE-060 calendar items API. Acceptance: ownership tests. Needs the test stack (tests/stack.py)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from tests.stack import Actor, Pair, needs_stack, sql
from tests.test_events import event

URL = "/api/v1/calendar-items"
T0 = datetime(2026, 12, 1, 4, 30, tzinfo=UTC)
WINDOW = {"from": "2026-11-01T00:00:00Z", "to": "2027-01-01T00:00:00Z"}


@pytest.fixture
def clean(stack_client: TestClient) -> TestClient:
    sql("TRUNCATE events, organisations CASCADE")
    return stack_client


def create(c: TestClient, who: Actor, **over: object) -> dict[str, object]:
    body = {
        "title": "Lab review",
        "starts_at": T0.isoformat(),
        "ends_at": (T0 + timedelta(hours=1)).isoformat(),
    }
    r = c.post(URL, headers=who.headers, json=body | over)
    assert r.status_code == 201, r.text
    out: dict[str, object] = r.json()
    return out


def listed(c: TestClient, who: Actor, **params: str) -> list[dict[str, object]]:
    r = c.get(URL, headers=who.headers, params=WINDOW | params)
    assert r.status_code == 200, r.text
    items: list[dict[str, object]] = r.json()["items"]
    return items


@needs_stack
def test_crud_round_trip_in_utc(clean: TestClient, two_users: Pair) -> None:
    a, _ = two_users
    item = create(clean, a, starts_at="2026-12-01T10:00:00+05:30", color="#1A2B3C")
    assert item["event_id"] is None and item["starts_at"] == "2026-12-01T04:30:00Z"
    assert [i["id"] for i in listed(clean, a)] == [item["id"]]
    assert listed(clean, a, **{"from": "2026-12-02T00:00:00Z"}) == []  # window filter

    moved = {"starts_at": "2026-12-03T04:30:00Z", "ends_at": "2026-12-03T07:30:00Z"}
    r = clean.patch(f"{URL}/{item['id']}", headers=a.headers, json=moved)  # drag + resize
    assert r.status_code == 200 and r.json()["ends_at"] == "2026-12-03T07:30:00Z"
    assert r.json()["title"] == "Lab review"  # untouched fields stay

    r = clean.patch(
        f"{URL}/{item['id']}", headers=a.headers, json={"ends_at": "2026-12-01T00:00:00Z"}
    )
    assert r.status_code == 422  # merged end before start
    for bad in (
        {"title": ""},
        {"title": None},
        {"color": "red"},
        {"starts_at": "2026-12-01T00:00:00"},
    ):
        assert clean.patch(f"{URL}/{item['id']}", headers=a.headers, json=bad).status_code == 422
    r = clean.post(
        URL,
        headers=a.headers,
        json={"title": "x", "starts_at": "2026-12-02T00:00:00Z", "ends_at": "2026-12-01T00:00:00Z"},
    )
    assert r.status_code == 422

    assert clean.delete(f"{URL}/{item['id']}", headers=a.headers).status_code == 204
    assert listed(clean, a) == []
    assert clean.delete(f"{URL}/{item['id']}", headers=a.headers).status_code == 404


@needs_stack
def test_b_cannot_see_or_touch_a_items(clean: TestClient, two_users: Pair) -> None:
    a, b = two_users
    item = create(clean, a)
    url = f"{URL}/{item['id']}"
    assert listed(clean, b) == []
    assert clean.patch(url, headers=b.headers, json={"title": "pwned"}).status_code == 404
    assert clean.delete(url, headers=b.headers).status_code == 404
    rows = sql("SELECT title, user_id::text FROM calendar_items")
    assert rows == [("Lab review", a.id)]  # unchanged, still A's
    assert clean.get(URL, params=WINDOW).status_code == 401
    assert (
        clean.patch(f"{URL}/{uuid.uuid4()}", headers=a.headers, json={"title": "x"}).status_code
        == 404
    )


@needs_stack
def test_registration_adds_and_removes_own_calendar_copy(
    clean: TestClient, two_users: Pair
) -> None:
    a, b = two_users
    eid = event("Hack <b>Day</b>", T0)  # test_events.event: event_start = deadline, no end
    reg = f"/api/v1/events/{eid}/registration"
    for _ in range(2):  # idempotent: one copy, not two
        assert clean.post(reg, headers=a.headers).status_code == 200
    (item,) = listed(clean, a)
    assert item["event_id"] == eid and item["title"] == "Hack Day"
    assert item["starts_at"] == "2026-12-01T04:30:00Z" and item["ends_at"] == "2026-12-01T05:30:00Z"
    assert listed(clean, b) == []

    # Dragging the copy never changes the event (doc 05).
    r = clean.patch(
        f"{URL}/{item['id']}",
        headers=a.headers,
        json={"starts_at": "2026-12-05T04:30:00Z", "ends_at": "2026-12-05T05:30:00Z"},
    )
    assert r.status_code == 200
    assert sql("SELECT event_start FROM events WHERE id = CAST(:e AS uuid)", e=eid) == [(T0,)]

    assert clean.post(reg, headers=b.headers).status_code == 200
    assert clean.delete(reg, headers=b.headers).status_code == 200  # B's unregister: B's copy only
    assert [i["id"] for i in listed(clean, a)] == [item["id"]] and listed(clean, b) == []
    assert clean.delete(reg, headers=a.headers).status_code == 200
    assert listed(clean, a) == []

    undated = event("No dates", None)
    assert (
        clean.post(f"/api/v1/events/{undated}/registration", headers=a.headers).status_code == 200
    )
    assert listed(clean, a) == []  # nothing to place; the feed still shows it as registered
