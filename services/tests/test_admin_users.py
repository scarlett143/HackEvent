"""HE-090: admin users list, role/active changes, invite list. Needs the test stack; see
tests/stack.py."""

from typing import Any

from fastapi.testclient import TestClient
from tests.stack import Actor, make_actor, needs_stack, sql
from tests.test_invites import accept, invite

from api.auth import hash_token

pytestmark = needs_stack

USERS = "/api/v1/admin/users"
INVITES = "/api/v1/admin/invites"


def patch(c: TestClient, actor: Actor, user_id: str, body: dict[str, Any]) -> Any:
    return c.patch(f"{USERS}/{user_id}", json=body, headers=actor.headers)


def audit() -> list[tuple[Any, ...]]:
    return sql(
        "SELECT action, actor_id::text, target_id::text, meta::text FROM audit_log"
        " WHERE action LIKE 'user.%' ORDER BY created_at"
    )


def test_non_admin_and_unverified_admin_are_refused(stack_client: TestClient) -> None:
    prof = make_actor("prof@example.com")
    mgmt = make_actor("mgmt@example.com", "management")
    for actor in (prof, mgmt):  # cross-user: a signed-in non-admin can't list or change anyone
        for r in (
            stack_client.get(USERS, headers=actor.headers),
            stack_client.get(INVITES, headers=actor.headers),
            patch(stack_client, actor, prof.id, {"role": "admin"}),
            patch(stack_client, actor, actor.id, {"role": "admin"}),
        ):
            assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    admin = make_actor("admin@example.com", "admin")
    sql("UPDATE sessions SET totp_verified_at = NULL")
    for r in (
        stack_client.get(USERS, headers=admin.headers),
        stack_client.get(INVITES, headers=admin.headers),
        patch(stack_client, admin, prof.id, {"is_active": False}),
    ):
        assert r.status_code == 403 and r.json()["error"]["code"] == "totp_required"
    sql("UPDATE users SET totp_enabled_at = NULL")
    r = stack_client.get(USERS, headers=admin.headers)
    assert r.json()["error"]["code"] == "totp_enrolment_required"
    assert stack_client.get(USERS).status_code == 401
    assert sql("SELECT role::text, is_active FROM users WHERE email = 'prof@example.com'") == [
        ("professor", True)
    ]
    assert audit() == []


def test_list_users_shape_without_secrets(stack_client: TestClient) -> None:
    admin = make_actor("admin@example.com", "admin")
    prof = make_actor("prof@example.com")
    r = stack_client.get(USERS, headers=admin.headers)
    assert r.status_code == 200
    body = r.json()
    assert body["next_cursor"] is None
    assert [u["email"] for u in body["items"]] == ["admin@example.com", "prof@example.com"]
    assert set(body["items"][1]) == {"id", "email", "name", "role", "is_active", "created_at"}
    assert body["items"][1] | {"created_at": None} == {
        "id": prof.id,
        "email": "prof@example.com",
        "name": "prof",
        "role": "professor",
        "is_active": True,
        "created_at": None,
    }
    for secret in ("argon2", hash_token(admin.token), hash_token(prof.token)):
        assert secret not in r.text


def test_role_change_and_deactivate_audit_and_end_sessions(stack_client: TestClient) -> None:
    admin = make_actor("admin@example.com", "admin")
    prof = make_actor("prof@example.com")
    assert stack_client.get("/api/v1/me", headers=prof.headers).status_code == 200

    r = patch(stack_client, admin, prof.id, {"role": "management"})
    assert r.status_code == 200 and r.json()["role"] == "management"
    assert stack_client.get("/api/v1/me", headers=prof.headers).json()["role"] == "management"
    assert patch(stack_client, admin, prof.id, {"role": "management"}).status_code == 200  # no-op

    r = patch(stack_client, admin, prof.id, {"is_active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert sql("SELECT count(*) FROM sessions WHERE user_id = CAST(:u AS uuid)", u=prof.id) == [
        (0,)
    ]
    assert stack_client.get("/api/v1/me", headers=prof.headers).status_code == 401
    # The admin's own session is untouched.
    assert stack_client.get(USERS, headers=admin.headers).status_code == 200

    r = patch(stack_client, admin, prof.id, {"is_active": True})
    assert r.json()["is_active"] is True
    assert audit() == [
        ("user.role_change", admin.id, prof.id, '{"to": "management", "from": "professor"}'),
        ("user.deactivate", admin.id, prof.id, '{"is_active": false}'),
        ("user.deactivate", admin.id, prof.id, '{"is_active": true}'),
    ]


def test_admin_cannot_demote_or_deactivate_self(stack_client: TestClient) -> None:
    admin = make_actor("admin@example.com", "admin")
    for body in (
        {"role": "professor"},
        {"is_active": False},
        {"role": "admin", "is_active": False},
    ):
        r = patch(stack_client, admin, admin.id, body)
        assert r.status_code == 409 and r.json()["error"]["code"] == "cannot_change_self"
    assert patch(stack_client, admin, admin.id, {"role": "admin"}).status_code == 200
    assert sql("SELECT role::text, is_active FROM users") == [("admin", True)]
    assert stack_client.get(USERS, headers=admin.headers).status_code == 200
    assert audit() == []
    # Another admin may demote this one.
    other = make_actor("other@example.com", "admin")
    assert patch(stack_client, other, admin.id, {"role": "professor"}).status_code == 200


def test_patch_unknown_user_and_bad_role(stack_client: TestClient) -> None:
    admin = make_actor("admin@example.com", "admin")
    r = patch(stack_client, admin, "00000000-0000-0000-0000-000000000000", {"role": "admin"})
    assert r.status_code == 404
    assert patch(stack_client, admin, admin.id, {"role": "owner"}).status_code == 422


def test_list_invites_status_and_no_token(stack_client: TestClient) -> None:
    admin = make_actor("admin@example.com", "admin")
    used = invite(stack_client, admin, "used@example.com")
    pending = invite(stack_client, admin, "pending@example.com", "management")
    expired = invite(stack_client, admin, "expired@example.com")
    assert accept(stack_client, used).status_code == 201
    sql(
        "UPDATE invites SET expires_at = now() - interval '1 second' WHERE email = 'expired@example.com'"
    )
    stack_client.cookies.clear()

    r = stack_client.get(INVITES, headers=admin.headers)
    assert r.status_code == 200
    rows = {i["email"]: i for i in r.json()["items"]}
    assert {e: i["status"] for e, i in rows.items()} == {
        "used@example.com": "used",
        "pending@example.com": "pending",
        "expired@example.com": "expired",
    }
    assert set(rows["pending@example.com"]) == {
        "id",
        "email",
        "role",
        "created_at",
        "expires_at",
        "used_at",
        "status",
    }
    assert rows["pending@example.com"]["role"] == "management"
    assert rows["used@example.com"]["used_at"] is not None
    for token in (used, pending, expired):
        assert token not in r.text and hash_token(token) not in r.text
