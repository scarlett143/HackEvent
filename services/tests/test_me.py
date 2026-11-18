"""HE-077: settings API (profile, prefs, password change, channel Send test). Needs the
test stack; see tests/stack.py."""

import pytest
from fastapi.testclient import TestClient
from tests.stack import GOOD_PASSWORD, Actor, Pair, make_actor, needs_stack, sql
from tests.test_invites import csrf, queued_emails

from api.auth import SESSION_COOKIE, hash_token

pytestmark = needs_stack

PREFS = "/api/v1/me/notification-prefs"
NEW_PASSWORD = "a much longer new passphrase"


def _prefs(**over: object) -> dict[str, object]:
    return {
        "deadline_offsets_days": [3, 1],
        "event_offsets_days": [0],
        "channels": {"email": True, "in_app": False},
    } | over


def test_prefs_defaults_when_no_row(stack_client: TestClient, two_users: Pair) -> None:
    a, _ = two_users
    r = stack_client.get(PREFS, headers=a.headers)
    assert r.status_code == 200
    assert r.json() == {
        "deadline_offsets_days": [3, 2, 1, 0],
        "event_offsets_days": [3, 2, 1, 0],
        "channels": {"email": True, "in_app": True},
        "new_event_alerts": True,  # HE-078
        "new_event_filters": {"region": [], "kind": [], "mode": []},
    }
    me = stack_client.get("/api/v1/me", headers=a.headers).json()
    assert (me["timezone"], me["quiet_start"], me["quiet_end"]) == ("Asia/Kolkata", None, None)


def test_prefs_put_dedupes_sorts_and_persists(stack_client: TestClient, two_users: Pair) -> None:
    a, b = two_users
    body = _prefs(deadline_offsets_days=[1, 7, 1, 0, 30])
    r = stack_client.put(PREFS, json=body, headers=a.headers)
    assert r.status_code == 200, r.text
    assert r.json()["deadline_offsets_days"] == [30, 7, 1, 0]
    assert stack_client.get(PREFS, headers=a.headers).json() == r.json()
    # second PUT updates the same row
    stack_client.put(PREFS, json=_prefs(event_offsets_days=[2]), headers=a.headers)
    assert sql("SELECT event_offsets_days FROM notification_prefs") == [([2],)]
    # cross-user: B still sees defaults and cannot reach A's row
    assert stack_client.get(PREFS, headers=b.headers).json()["event_offsets_days"] == [3, 2, 1, 0]


def test_new_event_alert_prefs_round_trip_and_are_kept(
    stack_client: TestClient, two_users: Pair
) -> None:
    a, b = two_users
    filters = {"region": ["india"], "kind": [], "mode": ["online", "hybrid"]}
    body = _prefs(
        new_event_alerts=False,
        new_event_filters={"region": ["india"], "mode": ["online", "hybrid"]},
    )
    r = stack_client.put(PREFS, json=body, headers=a.headers)
    assert r.status_code == 200, r.text
    assert (r.json()["new_event_alerts"], r.json()["new_event_filters"]) == (False, filters)
    stack_client.put(PREFS, json=_prefs(event_offsets_days=[2]), headers=a.headers)  # omitted
    got = stack_client.get(PREFS, headers=a.headers).json()
    assert (got["new_event_alerts"], got["new_event_filters"]) == (False, filters)
    assert sql("SELECT new_event_alerts, new_event_filters FROM notification_prefs") == [
        (False, filters)
    ]
    assert stack_client.get(PREFS, headers=b.headers).json()["new_event_alerts"] is True


@pytest.mark.parametrize(
    "body",
    [
        _prefs(deadline_offsets_days=[31]),
        _prefs(event_offsets_days=[-1]),
        _prefs(channels={"sms": True}),
        _prefs(channels={"email": "yes please"}),
        {"deadline_offsets_days": [1], "channels": {}},  # missing field (PUT is full)
        _prefs(new_event_filters={"region": ["mars"]}),  # HE-078: feed values only
        _prefs(new_event_filters={"tier": [1]}),
        _prefs(new_event_alerts="sometimes"),
    ],
)
def test_prefs_validation(stack_client: TestClient, two_users: Pair, body: object) -> None:
    a, _ = two_users
    r = stack_client.put(PREFS, json=body, headers=a.headers)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"
    assert sql("SELECT count(*) FROM notification_prefs") == [(0,)]


def test_patch_me_profile_and_quiet_hours(stack_client: TestClient, two_users: Pair) -> None:
    a, b = two_users
    body = {
        "name": "  Prof A ",
        "timezone": "Europe/Berlin",
        "quiet_start": "22:30",
        "quiet_end": "07:00",  # crosses midnight
    }
    r = stack_client.patch("/api/v1/me", json=body, headers=a.headers)
    assert r.status_code == 200, r.text
    assert r.json() | {"id": None} == {
        "id": None,
        "email": a.email,
        "name": "Prof A",
        "role": "professor",
        "timezone": "Europe/Berlin",
        "quiet_start": "22:30",
        "quiet_end": "07:00",
    }
    # partial update leaves other fields; turning quiet hours off
    r = stack_client.patch(
        "/api/v1/me", json={"quiet_start": None, "quiet_end": None}, headers=a.headers
    )
    assert (r.json()["quiet_start"], r.json()["timezone"]) == (None, "Europe/Berlin")
    # cross-user: B unchanged
    me_b = stack_client.get("/api/v1/me", headers=b.headers).json()
    assert (me_b["name"], me_b["timezone"]) == ("b", "Asia/Kolkata")


@pytest.mark.parametrize(
    "body",
    [
        {"timezone": "Mars/Olympus"},
        {"timezone": "../../etc/passwd"},
        {"name": "   "},
        {"quiet_start": "22:00"},  # end missing
        {"quiet_start": "22:00", "quiet_end": None},
        {"quiet_start": "24:00", "quiet_end": "07:00"},
        {"quiet_start": "7:00", "quiet_end": "08:00"},
        {"quiet_start": "08:00", "quiet_end": "08:00"},
    ],
)
def test_patch_me_validation(stack_client: TestClient, two_users: Pair, body: object) -> None:
    a, _ = two_users
    r = stack_client.patch("/api/v1/me", json=body, headers=a.headers)
    assert r.status_code == 422, r.text
    assert sql("SELECT timezone, quiet_start FROM users WHERE email = :e", e=a.email) == [
        ("Asia/Kolkata", None)
    ]


def _second_session(actor: Actor) -> Actor:
    token = "second-session-token-for-tests-0123456789"
    sql(
        "INSERT INTO sessions (user_id, token_hash, expires_at, ip, user_agent)"
        " VALUES (CAST(:u AS uuid), :h, now() + interval '14 days', 'x', 'other device')",
        u=actor.id,
        h=hash_token(token),
    )
    return Actor(id=actor.id, email=actor.email, role=actor.role, token=token)


def _pw(c: TestClient, actor: Actor, current: str, new: str) -> int:
    body = {"current_password": current, "new_password": new}
    return c.post("/api/v1/me/password", json=body, headers=actor.headers).status_code


def test_password_change_revokes_other_sessions(stack_client: TestClient, two_users: Pair) -> None:
    a, b = two_users
    other = _second_session(a)
    assert _pw(stack_client, a, GOOD_PASSWORD, NEW_PASSWORD) == 204
    assert stack_client.get("/api/v1/me", headers=a.headers).status_code == 200  # kept
    assert stack_client.get("/api/v1/me", headers=other.headers).status_code == 401  # revoked
    assert stack_client.get("/api/v1/me", headers=b.headers).status_code == 200  # B untouched
    for password, status in ((GOOD_PASSWORD, 401), (NEW_PASSWORD, 200)):
        r = stack_client.post(
            "/api/v1/auth/login",
            json={"email": a.email, "password": password},
            headers=csrf(stack_client),
        )
        assert r.status_code == status
        stack_client.cookies.delete(SESSION_COOKIE)


def test_password_change_rejections(stack_client: TestClient, two_users: Pair) -> None:
    a, _ = two_users
    other = _second_session(a)
    assert _pw(stack_client, a, "not my password", NEW_PASSWORD) == 400
    assert _pw(stack_client, a, GOOD_PASSWORD, "short") == 422
    assert _pw(stack_client, a, GOOD_PASSWORD, "password1234") == 422  # common list
    # nothing changed: other session alive, old password still the one
    assert stack_client.get("/api/v1/me", headers=other.headers).status_code == 200
    assert _pw(stack_client, a, GOOD_PASSWORD, NEW_PASSWORD) == 204


def test_password_wrong_current_is_rate_limited(stack_client: TestClient) -> None:
    a = make_actor("rl@example.com")
    for _ in range(5):
        assert _pw(stack_client, a, "wrong wrong wrong", NEW_PASSWORD) == 400
    assert _pw(stack_client, a, GOOD_PASSWORD, NEW_PASSWORD) == 429


def test_send_test_email_enqueues_one_job(stack_client: TestClient, two_users: Pair) -> None:
    a, _ = two_users
    r = stack_client.post("/api/v1/channels/email/test", headers=a.headers)
    assert r.status_code == 202
    assert r.json() == {"channel": "email", "status": "queued"}
    assert queued_emails() == [("send_email", a.email, "test", {})]


def test_send_test_in_app_inserts_one_row(stack_client: TestClient, two_users: Pair) -> None:
    a, b = two_users
    r = stack_client.post("/api/v1/channels/in_app/test", headers=a.headers)
    assert r.json() == {"channel": "in_app", "status": "delivered"}
    rows = sql("SELECT user_id::text, type FROM notifications")
    assert rows == [(a.id, "test")]
    count = stack_client.get("/api/v1/me/unread-count", headers=a.headers).json()
    assert count["notifications"] == 1
    assert (
        stack_client.get("/api/v1/me/unread-count", headers=b.headers).json()["notifications"] == 0
    )
    assert queued_emails() == []


def test_send_test_unknown_channel(stack_client: TestClient, two_users: Pair) -> None:
    a, _ = two_users
    for ch in ("telegram", "sms"):
        assert (
            stack_client.post(f"/api/v1/channels/{ch}/test", headers=a.headers).status_code == 422
        )


def test_send_test_rate_limit_per_user(stack_client: TestClient, two_users: Pair) -> None:
    a, b = two_users
    for _ in range(5):
        assert (
            stack_client.post("/api/v1/channels/in_app/test", headers=a.headers).status_code == 202
        )
    r = stack_client.post("/api/v1/channels/email/test", headers=a.headers)
    assert r.status_code == 429
    assert 0 < int(r.headers["Retry-After"]) <= 3600
    assert queued_emails() == []
    # per user: B is not limited by A
    assert stack_client.post("/api/v1/channels/in_app/test", headers=b.headers).status_code == 202


def test_settings_routes_need_a_session(stack_client: TestClient) -> None:
    h = csrf(stack_client)
    for method, path in (
        ("GET", PREFS),
        ("PUT", PREFS),
        ("PATCH", "/api/v1/me"),
        ("POST", "/api/v1/me/password"),
        ("POST", "/api/v1/channels/email/test"),
    ):
        assert stack_client.request(method, path, json={}, headers=h).status_code == 401
