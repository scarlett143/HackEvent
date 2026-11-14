"""Shared helpers for tests against a real Postgres + Redis (HE-013).

Tests using these are skipped unless TEST_DATABASE_URL and TEST_REDIS_URL point at a
THROWAWAY Postgres 16 and Redis: the DB is migrated to head and `users` is truncated
(cascading to every user-owned table), Redis is FLUSHDB'd. Example:

    docker run -d --rm --name he-pg -e POSTGRES_PASSWORD=test -e POSTGRES_USER=hackevents \
        -e POSTGRES_DB=hackevents -p 55433:5432 postgres:16-alpine
    docker run -d --rm --name he-redis -p 56380:6379 redis:7-alpine
    TEST_DATABASE_URL=postgresql+asyncpg://hackevents:test@localhost:55433/hackevents \
    TEST_REDIS_URL=redis://localhost:56380/0 uv run pytest

Cross-user test template (Security doc §3: every endpoint gets one). The `stack_client`
and `two_users` fixtures live in conftest.py:

    @needs_stack
    def test_b_cannot_touch_a_rows(stack_client: TestClient, two_users: Pair) -> None:
        a, b = two_users
        row = create_row_for(a)                       # via the API as `a`, or sql()
        assert stack_client.get(f"/api/v1/things/{row}", headers=a.headers).status_code == 200
        for method in ("GET", "PATCH", "DELETE"):     # every verb the route supports
            r = stack_client.request(method, f"/api/v1/things/{row}", headers=b.headers)
            assert r.status_code == 404               # other users' rows: 404, not 403
        assert row not in stack_client.get("/api/v1/things", headers=b.headers).text
        # ...and `row` is unchanged in the DB afterwards (check with sql()).

`tests/test_access.py` runs this template for real against `get_owned`.
"""

import asyncio
import os
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import redis
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from api.auth import SESSION_COOKIE, hash_password, hash_token
from api.csrf import CSRF_COOKIE, CSRF_HEADER, csrf_for
from common.config import get_settings

DB_URL = os.environ.get("TEST_DATABASE_URL")
REDIS_URL = os.environ.get("TEST_REDIS_URL")
GOOD_PASSWORD = "correct horse battery staple"

needs_stack = pytest.mark.skipif(
    not (DB_URL and REDIS_URL), reason="TEST_DATABASE_URL / TEST_REDIS_URL not set"
)


def sql(statement: str, **params: Any) -> list[Any]:
    """Run one statement in its own transaction; return its rows (if any)."""

    async def run() -> list[Any]:
        assert DB_URL
        engine = create_async_engine(DB_URL)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(statement), params)
                return list(result.all()) if result.returns_rows else []
        finally:
            await engine.dispose()

    return asyncio.run(run())


@dataclass(frozen=True)
class Actor:
    """A signed-in user: send `headers` with a request to act as them."""

    id: str
    email: str
    role: str
    token: str

    @property
    def headers(self) -> dict[str, str]:
        """Session cookie plus the session-bound CSRF double-submit pair (HE-015), so the
        same headers work for reads and writes (same as test_auth.writes_as)."""
        csrf = csrf_for(self.token)
        return {
            "Cookie": f"{SESSION_COOKIE}={self.token}; {CSRF_COOKIE}={csrf}",
            CSRF_HEADER: csrf,
        }


Pair = tuple[Actor, Actor]


def make_actor(email: str, role: str = "professor", password: str = GOOD_PASSWORD) -> Actor:
    """Insert an active user plus a live session directly (no login, so no rate limit
    or TOTP in the way) and return it as an Actor."""
    (uid,) = sql(
        "INSERT INTO users (email, name, role, password_hash)"
        " VALUES (:e, :n, CAST(:r AS user_role), :h) RETURNING id::text",
        e=email,
        n=email.split("@")[0],
        r=role,
        h=hash_password(password),
    )[0]
    token = secrets.token_urlsafe(32)
    # Full 14-day expiry: a nearer one makes current_auth refresh the session and set the
    # cookie, which the TestClient jar would then send on later "anonymous" requests.
    sql(
        "INSERT INTO sessions (user_id, token_hash, expires_at, ip, user_agent)"
        " VALUES (CAST(:u AS uuid), :h, now() + interval '14 days', 'testclient', 'pytest')",
        u=uid,
        h=hash_token(token),
    )
    return Actor(id=uid, email=email, role=role, token=token)


ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def open_stack(monkeypatch: pytest.MonkeyPatch, app: FastAPI) -> Iterator[TestClient]:
    """Point settings at the test stack, migrate, wipe users + Redis, yield a client."""
    assert DB_URL and REDIS_URL
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    get_settings.cache_clear()
    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["skip_logging"] = True
    cfg.attributes["database_url"] = DB_URL
    command.upgrade(cfg, "head")
    sql("TRUNCATE users CASCADE")
    redis.Redis.from_url(REDIS_URL).flushdb()
    # https base URL: the session cookie is Secure.
    with TestClient(app, base_url="https://testserver") as c:
        yield c
    get_settings.cache_clear()
