"""HE-010: invite create + email + accept (Security doc §4). Needs the test stack; see
tests/stack.py. The invite email is really enqueued; tests read the token back from the
queued `send_email` job, exactly as the worker would."""

import asyncio
from datetime import timedelta
from typing import Any

import httpx2
import pytest
from arq.connections import ArqRedis
from fastapi.testclient import TestClient
from redis.asyncio import ConnectionPool
from tests.stack import REDIS_URL, Actor, make_actor, needs_stack, sql

from api import health, invites
from api.auth import SESSION_COOKIE, hash_token
from api.csrf import CSRF_HEADER
from api.main import app
from common.queues import NOTIFY_QUEUE

pytestmark = needs_stack

PASSWORD = "correct horse battery staple"
INVALID = {
    "error": {
        "code": "invalid_invite",
        "message": "This invite link is invalid or has expired.",
        "details": {},
    }
}


def queued_emails() -> list[tuple[Any, ...]]:
    async def run() -> list[tuple[Any, ...]]:
        assert REDIS_URL
        pool = ConnectionPool.from_url(REDIS_URL)
        try:
            jobs = await ArqRedis(pool).queued_jobs(queue_name=NOTIFY_QUEUE)
            return [(j.function, *j.args) for j in jobs]
        finally:
            await pool.aclose()

    return asyncio.run(run())


def invite(c: TestClient, admin: Actor, email: str, role: str = "professor") -> str:
    """Create an invite as `admin`; return the token from its queued email job."""
    r = c.post("/api/v1/admin/invites", json={"email": email, "role": role}, headers=admin.headers)
    assert r.status_code == 201, r.text
    [(stored,)] = sql(
        "SELECT token_hash FROM invites WHERE id = CAST(:i AS uuid)", i=r.json()["id"]
    )
    [token] = [j[3]["token"] for j in queued_emails() if hash_token(j[3]["token"]) == stored]
    return str(token)


def csrf(c: TestClient) -> dict[str, str]:
    """Pre-login write: fetch a CSRF token (sets the cookie) and echo it, like the web app."""
    return {CSRF_HEADER: c.get("/api/v1/auth/csrf").json()["csrf_token"]}


def accept(c: TestClient, token: str, password: str = PASSWORD, name: str = "New Person") -> Any:
    c.cookies.clear()
    return c.post(
        f"/api/v1/invites/{token}/accept",
        json={"name": name, "password": password},
        headers=csrf(c),
    )


def audit_rows() -> list[tuple[Any, ...]]:
    return sql("SELECT action, actor_id::text, target_id::text, meta::text FROM audit_log")


def test_non_admin_cannot_create_invite(stack_client: TestClient) -> None:
    for role in ("professor", "management"):
        actor = make_actor(f"{role}@example.com", role)
        r = stack_client.post(
            "/api/v1/admin/invites",
            json={"email": "x@example.com", "role": "admin"},
            headers=actor.headers,
        )
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "forbidden"
    # An admin whose session has not passed TOTP is refused too (HE-014).
    admin = make_actor("admin@example.com", "admin")
    sql("UPDATE sessions SET totp_verified_at = NULL")
    r = stack_client.post(
        "/api/v1/admin/invites",
        json={"email": "x@example.com", "role": "admin"},
        headers=admin.headers,
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "totp_required"
    stack_client.cookies.clear()
    anon = stack_client.post(
        "/api/v1/admin/invites",
        json={"email": "x@example.com", "role": "admin"},
        headers=csrf(stack_client),
    )
    assert anon.status_code == 401, anon.text
    assert sql("SELECT count(*) FROM invites") == [(0,)]
    assert queued_emails() == []
    assert audit_rows() == []


def test_create_stores_only_hash_enqueues_email_and_audits(stack_client: TestClient) -> None:
    admin = make_actor("admin@example.com", "admin")
    r = stack_client.post(
        "/api/v1/admin/invites",
        json={"email": " New.Person@Example.COM ", "role": "management"},
        headers=admin.headers,
    )
    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"id", "email", "role", "expires_at"}
    assert body["email"] == "new.person@example.com" and body["role"] == "management"

    [(fn, to, template, ctx)] = queued_emails()
    assert (fn, to, template) == ("send_email", "new.person@example.com", "invite")
    token = ctx["token"]
    assert ctx == {"token": token, "inviter": "admin"}
    assert len(token) >= 43  # 32 random bytes, urlsafe base64
    assert token not in r.text

    [row] = sql("SELECT row_to_json(i)::text, token_hash, expires_at - created_at FROM invites i")
    assert token not in row[0]
    assert row[1] == hash_token(token)
    assert abs(row[2] - timedelta(hours=72)) < timedelta(seconds=5)

    [(action, actor, target, meta)] = audit_rows()
    assert (action, actor, target) == ("invite.create", admin.id, body["id"])
    assert token not in meta and row[1] not in meta


def test_accept_once_then_link_is_dead(stack_client: TestClient) -> None:
    admin = make_actor("admin@example.com", "admin")
    token = invite(stack_client, admin, "new@example.com", "management")
    assert stack_client.get(f"/api/v1/invites/{token}").json() == {
        "email": "new@example.com",
        "role": "management",
    }

    r = accept(stack_client, token)
    assert r.status_code == 201, r.text
    me = r.json()
    assert (me["email"], me["name"], me["role"]) == ("new@example.com", "New Person", "management")
    assert me["timezone"] == "Asia/Kolkata"
    session = stack_client.cookies[SESSION_COOKIE]
    assert sql("SELECT token_hash FROM sessions WHERE user_id = CAST(:u AS uuid)", u=me["id"]) == [
        (hash_token(session),)
    ]
    assert stack_client.get("/api/v1/me").json()["id"] == me["id"]
    # The new password works for normal login.
    stack_client.cookies.clear()
    login = stack_client.post(
        "/api/v1/auth/login",
        json={"email": "NEW@example.com", "password": PASSWORD},
        headers=csrf(stack_client),
    )
    assert login.status_code == 200

    second = accept(stack_client, token, name="Attacker")
    assert second.status_code == 404 and second.json() == INVALID
    assert stack_client.get(f"/api/v1/invites/{token}").json() == INVALID
    unknown = accept(stack_client, "x" * 43)
    assert unknown.status_code == 404 and unknown.text == second.text
    assert sql("SELECT count(*) FROM users WHERE email = 'new@example.com'") == [(1,)]
    assert sql("SELECT used_at IS NOT NULL FROM invites") == [(True,)]
    actions = [(a, actor) for a, actor, _, _ in audit_rows()]
    assert actions == [("invite.create", admin.id), ("invite.accept", me["id"])]
    for row in audit_rows():
        assert token not in row[3] and hash_token(token) not in row[3]
    # Single use is the invite's own state, not just users.email being unique: after the
    # account is deleted (doc 06 POST /me/delete) the old link still must not work.
    sql("DELETE FROM audit_log")
    sql("DELETE FROM users WHERE email = 'new@example.com'")
    assert accept(stack_client, token).json() == INVALID
    assert sql("SELECT count(*) FROM users WHERE email = 'new@example.com'") == [(0,)]


def test_expires_after_72_hours(stack_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    admin = make_actor("admin@example.com", "admin")
    token = invite(stack_client, admin, "late@example.com")
    (created,) = sql("SELECT created_at FROM invites")[0]

    monkeypatch.setattr(invites, "_now", lambda: created + timedelta(hours=72, minutes=-1))
    assert stack_client.get(f"/api/v1/invites/{token}").status_code == 200

    monkeypatch.setattr(invites, "_now", lambda: created + timedelta(hours=72, seconds=1))
    assert stack_client.get(f"/api/v1/invites/{token}").json() == INVALID
    r = accept(stack_client, token)
    assert r.status_code == 404 and r.json() == INVALID
    assert sql("SELECT count(*) FROM users WHERE email = 'late@example.com'") == [(0,)]
    assert sql("SELECT used_at FROM invites") == [(None,)]


def test_email_casing_existing_user_cannot_be_reinvited(stack_client: TestClient) -> None:
    admin = make_actor("admin@example.com", "admin")
    make_actor("prof@example.com")
    r = stack_client.post(
        "/api/v1/admin/invites",
        json={"email": "PROF@Example.com", "role": "professor"},
        headers=admin.headers,
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "already_registered"
    bad = stack_client.post(
        "/api/v1/admin/invites",
        json={"email": "not-an-email", "role": "professor"},
        headers=admin.headers,
    )
    assert bad.status_code == 422
    assert sql("SELECT count(*) FROM invites") == [(0,)]


def test_user_registered_meanwhile_gets_generic_error_and_link_survives(
    stack_client: TestClient,
) -> None:
    admin = make_actor("admin@example.com", "admin")
    token = invite(stack_client, admin, "dup@example.com")
    make_actor("DUP@example.com")  # citext: same address
    assert accept(stack_client, token).json() == INVALID
    assert sql("SELECT used_at FROM invites") == [(None,)]


def test_weak_password_does_not_consume_invite(stack_client: TestClient) -> None:
    admin = make_actor("admin@example.com", "admin")
    token = invite(stack_client, admin, "weak@example.com")
    r = accept(stack_client, token, password="password1234")
    assert r.status_code == 422 and r.json()["error"]["code"] == "weak_password"
    assert accept(stack_client, token, name="   ").status_code == 422
    assert accept(stack_client, token).status_code == 201


def test_concurrent_accepts_only_one_wins(stack_client: TestClient) -> None:
    admin = make_actor("admin@example.com", "admin")
    token = invite(stack_client, admin, "race@example.com")

    async def race() -> list[int]:
        async with health.lifespan(app):
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(transport=transport, base_url="https://testserver") as c:
                header = {CSRF_HEADER: (await c.get("/api/v1/auth/csrf")).json()["csrf_token"]}
                rs = await asyncio.gather(
                    *(
                        c.post(
                            f"/api/v1/invites/{token}/accept",
                            json={"name": f"n{i}", "password": PASSWORD},
                            headers=header,
                        )
                        for i in range(5)
                    )
                )
        return sorted(r.status_code for r in rs)

    assert asyncio.run(race()) == [201, 404, 404, 404, 404]
    assert sql("SELECT count(*) FROM users WHERE email = 'race@example.com'") == [(1,)]
    assert sql("SELECT count(*) FROM audit_log WHERE action = 'invite.accept'") == [(1,)]


def test_cross_user_new_session_is_only_the_invitees(stack_client: TestClient) -> None:
    admin = make_actor("admin@example.com", "admin")
    a = make_actor("a@example.com")
    token = invite(stack_client, admin, "b@example.com")
    b = accept(stack_client, token).json()
    stack_client.cookies.clear()
    assert stack_client.get("/api/v1/me", headers=a.headers).json()["id"] == a.id
    # A signed-in non-admin can't mint invites for others, even with a valid session.
    r = stack_client.post(
        "/api/v1/admin/invites", json={"email": "c@example.com", "role": "admin"}, headers=a.headers
    )
    assert r.status_code == 403
    assert sql("SELECT count(*) FROM sessions WHERE user_id = CAST(:u AS uuid)", u=b["id"]) == [
        (1,)
    ]
    assert sql("SELECT role::text FROM users WHERE id = CAST(:u AS uuid)", u=b["id"]) == [
        ("professor",)
    ]
