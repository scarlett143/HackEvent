"""HE-096 audit log viewer: admin-only, newest first, filter by actor/action, cursor."""

import json
import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.stack import Actor, Pair, make_actor, needs_stack, sql

from common.audit import AuditAction

URL = "/api/v1/admin/audit"


@pytest.fixture
def c(stack_client: TestClient) -> TestClient:
    sql("TRUNCATE audit_log")
    return stack_client


@pytest.fixture
def admin(c: TestClient) -> Actor:
    return make_actor(f"admin-{uuid.uuid4().hex[:6]}@example.com", "admin")


def audit(actor: str | None, action: str, minutes_ago: int, meta: object = None) -> str:
    return str(
        sql(
            "INSERT INTO audit_log (actor_id, action, target_type, target_id, meta, ip, at)"
            " VALUES (CAST(:a AS uuid), :act, 'user', NULL, CAST(:m AS jsonb), '10.0.0.1',"
            " now() - make_interval(mins => :ago)) RETURNING id::text",
            a=actor, act=action, m=json.dumps(meta or {}), ago=minutes_ago,
        )[0][0]
    )  # fmt: skip


@needs_stack
def test_audit_is_admin_only(c: TestClient, two_users: Pair) -> None:
    a, b = two_users
    audit(a.id, "invite.create", 1)
    for who in (a, b):
        r = c.get(URL, headers=who.headers)
        assert r.status_code == 403
        assert "invite.create" not in r.text
    assert c.get(URL).status_code == 401


@needs_stack
def test_audit_newest_first_with_actor_email(c: TestClient, admin: Actor, two_users: Pair) -> None:
    a, _ = two_users
    old = audit(admin.id, "invite.create", 30, {"email": "<b>x</b>@example.com"})
    new = audit(a.id, "user.role_change", 5)
    anon = audit(None, "auth.login_fail", 1)
    r = c.get(URL, headers=admin.headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["id"] for i in body["items"]] == [anon, new, old]
    assert body["next_cursor"] is None
    by_id = {i["id"]: i for i in body["items"]}
    assert by_id[old]["actor_email"] == admin.email
    assert by_id[old]["meta"] == {"email": "<b>x</b>@example.com"}  # JSON data, unescaped
    assert by_id[anon]["actor_id"] is None and by_id[anon]["actor_email"] is None


@needs_stack
def test_audit_filters_by_actor_and_action(c: TestClient, admin: Actor, two_users: Pair) -> None:
    a, b = two_users
    a1 = audit(a.id, "invite.create", 3)
    a2 = audit(a.id, "source.edit", 2)
    b1 = audit(b.id, "invite.create", 1)

    def ids(**params: str) -> list[str]:
        r = c.get(URL, params=params, headers=admin.headers)
        assert r.status_code == 200, r.text
        return [i["id"] for i in r.json()["items"]]

    assert ids(actor=a.id) == [a2, a1]
    assert ids(action="invite.create") == [b1, a1]
    assert ids(actor=a.id, action="invite.create") == [a1]
    assert ids(actor=str(uuid.uuid4())) == []
    assert c.get(URL, params={"action": "nope"}, headers=admin.headers).status_code == 422
    assert c.get(URL, params={"actor": "nope"}, headers=admin.headers).status_code == 422


@needs_stack
def test_audit_cursor_pages_without_gaps(c: TestClient, admin: Actor) -> None:
    rows = [audit(admin.id, "source.create", m) for m in (5, 4, 3, 2, 1)]
    seen: list[str] = []
    cursor = None
    while True:
        params = {"limit": "2"} | ({"cursor": cursor} if cursor else {})
        body = c.get(URL, params=params, headers=admin.headers).json()
        seen += [i["id"] for i in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == rows[::-1]
    assert c.get(URL, params={"cursor": "!!"}, headers=admin.headers).status_code == 400


def test_web_action_filter_matches_audit_action() -> None:
    """The web page's action dropdown mirrors AuditAction (no shared source)."""
    page = Path(__file__).resolve().parents[2] / "apps/web/src/app/admin/audit/page.tsx"
    block = page.read_text().split("const ACTIONS = [", 1)[1].split("]", 1)[0]
    assert re.findall(r'"([^"]+)"', block) == [a.value for a in AuditAction]
