"""HE-013: deny-by-default route check, require_role, row ownership (Security doc §3).

`test_every_api_route_has_an_access_dependency` is the CI check: a new /api route must
depend on `current_auth` (via `current_user` / `require_role`) or be added to PUBLIC.
"""

import uuid
from collections.abc import Callable, Iterator
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.stack import Pair, make_actor, needs_stack, open_stack, sql

from api import health
from api.auth import current_auth
from api.deps import get_db
from api.errors import ApiError, handle_api_error
from api.main import app
from api.roles import get_owned, owned, require_role
from common.models import Session, User, UserRole

# Routes reachable without a session. Every entry needs a reason; keep this list short.
PUBLIC = {
    ("GET", "/api/v1/health"),  # dependency status for monitors
    ("GET", "/api/v1/health/live"),  # Docker healthcheck
    ("POST", "/api/v1/auth/login"),  # obtains the session
}


def _calls(dep: Dependant | None) -> Iterator[Callable[..., Any]]:
    for sub in dep.dependencies if dep else ():
        if sub.call is not None:
            yield sub.call
        yield from _calls(sub)


def _api_routes() -> dict[tuple[str, str], Dependant | None]:
    """(method, path) -> effective dependant (incl. router/include_router dependencies) for
    every /api route. Non-FastAPI routes (mounts, plain Starlette) get method "*" and no
    dependant, so they fail the check unless listed."""
    out: dict[tuple[str, str], Dependant | None] = {}
    for ctx in iter_route_contexts(app.routes):  # app.routes alone hides included routers
        path = ctx.path or ""
        if path.startswith("/api"):
            for method in ctx.methods or {"*"}:
                out[(method, path)] = getattr(ctx, "dependant", None)
    return out


def test_every_api_route_has_an_access_dependency() -> None:
    routes = _api_routes()
    assert ("GET", "/api/v1/me") in routes  # the walk really sees included routers
    unguarded = {key for key, dep in routes.items() if current_auth not in set(_calls(dep))}
    assert unguarded - PUBLIC == set(), "add require_role/current_user, or justify in PUBLIC"


def test_public_allow_list_has_no_stale_entries() -> None:
    assert PUBLIC <= _api_routes().keys()


def test_require_role_needs_a_role() -> None:
    with pytest.raises(ValueError):
        require_role()


def test_owned_filters_by_user_unless_bypassed() -> None:
    prof = User(id=uuid.uuid4(), role=UserRole.professor)
    mgmt = User(id=uuid.uuid4(), role=UserRole.management)
    base = select(Session)
    scoped = owned(base, Session, prof, bypass=(UserRole.management,))
    assert "WHERE sessions.user_id = " in str(scoped)
    assert scoped.compile().params == {"user_id_1": prof.id}
    assert owned(base, Session, mgmt, bypass=(UserRole.management,)) is base


# --- against real Postgres + Redis, with `sessions` standing in for a user-owned table ---

demo = FastAPI(lifespan=health.lifespan)
demo.add_exception_handler(ApiError, handle_api_error)


@demo.get("/admin-only")
async def admin_only(user: Annotated[User, Depends(require_role(UserRole.admin))]) -> str:
    return user.email


@demo.get("/sessions/{session_id}")
async def read_session(
    session_id: uuid.UUID,
    user: Annotated[User, Depends(require_role(*UserRole))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> str:
    row = await get_owned(db, Session, session_id, user, bypass=(UserRole.admin,))
    return str(row.user_id)


@pytest.fixture
def demo_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    yield from open_stack(monkeypatch, demo)


def _session_of(actor_id: str) -> str:
    (sid,) = sql("SELECT id::text FROM sessions WHERE user_id = CAST(:u AS uuid)", u=actor_id)[0]
    return str(sid)


@needs_stack
def test_require_role_403_in_doc06_shape(demo_client: TestClient) -> None:
    prof = make_actor("prof@example.com")
    mgmt = make_actor("mgmt@example.com", "management")
    admin = make_actor("admin@example.com", "admin")
    for actor in (prof, mgmt):
        r = demo_client.get("/admin-only", headers=actor.headers)
        assert r.status_code == 403
        assert r.json() == {
            "error": {
                "code": "forbidden",
                "message": "You do not have access to this.",
                "details": {},
            }
        }
    assert demo_client.get("/admin-only", headers=admin.headers).json() == "admin@example.com"
    assert demo_client.get("/admin-only").status_code == 401


@needs_stack
def test_cross_user_template(demo_client: TestClient) -> None:
    """The tests/stack.py template, run for real: B can't read A's row; A and admin can."""
    a, b = make_actor("a@example.com"), make_actor("b@example.com")
    row_a = _session_of(a.id)
    assert demo_client.get(f"/sessions/{row_a}", headers=a.headers).json() == a.id
    r = demo_client.get(f"/sessions/{row_a}", headers=b.headers)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
    # Unknown id looks exactly like someone else's id.
    unknown = demo_client.get(f"/sessions/{uuid.uuid4()}", headers=b.headers)
    assert unknown.status_code == 404 and unknown.text == r.text
    admin = make_actor("admin@example.com", "admin")  # bypass role
    assert demo_client.get(f"/sessions/{row_a}", headers=admin.headers).json() == a.id


@needs_stack
def test_two_users_fixture_gives_distinct_signed_in_users(
    stack_client: TestClient, two_users: Pair
) -> None:
    a, b = two_users
    assert stack_client.get("/api/v1/me", headers=a.headers).json()["id"] == a.id
    assert stack_client.get("/api/v1/me", headers=b.headers).json()["id"] == b.id
