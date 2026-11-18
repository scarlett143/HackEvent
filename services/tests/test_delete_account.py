"""POST /me/delete: typed phrase + password, own account only, last admin kept, data gone."""

import uuid

from fastapi.testclient import TestClient
from tests.stack import GOOD_PASSWORD, Pair, make_actor, needs_stack, sql

URL = "/api/v1/me/delete"
OK = {"password": GOOD_PASSWORD, "confirm": "delete my account"}


def count(table: str, user: str) -> int:
    return int(sql(f"SELECT count(*) FROM {table} WHERE user_id = CAST(:u AS uuid)", u=user)[0][0])


@needs_stack
def test_needs_the_typed_phrase_and_the_password(stack_client: TestClient, two_users: Pair) -> None:
    a, _ = two_users
    for body, status in (
        ({**OK, "confirm": "delete"}, 422),
        ({**OK, "confirm": "delete my acount"}, 422),
        ({**OK, "password": "wrong-password-123"}, 400),
    ):
        assert stack_client.post(URL, json=body, headers=a.headers).status_code == status, body
    assert sql("SELECT count(*) FROM users WHERE id = CAST(:u AS uuid)", u=a.id) == [(1,)]
    assert stack_client.post(URL, json=OK).status_code in (401, 403)  # signed out: refused


@needs_stack
def test_deletes_own_account_and_data_only(stack_client: TestClient, two_users: Pair) -> None:
    a, b = two_users
    (ev,) = sql(
        "INSERT INTO events (title, organiser_name, description, registration_url, mode, status,"
        " review_status, confidence, dedupe_key) VALUES ('Hack', 'Org', '', 'https://example.com/r',"
        " 'online', 'open', 'auto', 0.9, :k) RETURNING id::text",
        k=uuid.uuid4().hex,
    )[0]
    for who in (a, b):
        stack_client.post(f"/api/v1/events/{ev}/registration", headers=who.headers)
        sql(
            "INSERT INTO notifications (user_id, type, title, body) VALUES (CAST(:u AS uuid), 't', 't', 'b')",
            u=who.id,
        )
    r = stack_client.post(URL, json={**OK, "confirm": "  Delete My Account "}, headers=a.headers)
    assert r.status_code == 204, r.text
    assert sql("SELECT count(*) FROM users WHERE id = CAST(:u AS uuid)", u=a.id) == [(0,)]
    for table in ("sessions", "registrations", "notifications"):
        assert count(table, a.id) == 0, table
        assert count(table, b.id) == 1 if table != "sessions" else count(table, b.id) >= 1
    assert stack_client.get("/api/v1/me", headers=a.headers).status_code == 401  # old session dead
    (audit,) = sql("SELECT actor_id, target_id::text FROM audit_log WHERE action = 'user.delete'")
    assert audit == (None, a.id)  # kept, actor nulled (doc 07)


@needs_stack
def test_admin_with_invites_can_delete_but_not_the_last_admin(stack_client: TestClient) -> None:
    sql("TRUNCATE invites")
    first = make_actor(f"adm1-{uuid.uuid4().hex[:6]}@example.com", "admin")
    sql(
        "UPDATE users SET is_active = false WHERE role = 'admin' AND id <> CAST(:u AS uuid)",
        u=first.id,
    )
    r = stack_client.post(URL, json=OK, headers=first.headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "last_admin"
    second = make_actor(f"adm2-{uuid.uuid4().hex[:6]}@example.com", "admin")
    r = stack_client.post(
        "/api/v1/admin/invites",
        json={"email": "new@example.com", "role": "professor"},
        headers=first.headers,
    )
    assert r.status_code in (200, 201), r.text
    assert (
        stack_client.post(URL, json=OK, headers=first.headers).status_code == 204
    )  # invites gone too
    assert sql("SELECT count(*) FROM users WHERE id = CAST(:u AS uuid)", u=second.id) == [(1,)]
