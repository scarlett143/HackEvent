"""HE-011: password login, Argon2id, sessions, logout, login rate limit.

Policy/hashing tests run everywhere. Endpoint tests need a THROWAWAY Postgres 16 and Redis
(the DB is migrated to head and `users` is truncated; Redis is FLUSHDB'd) and are skipped
unless both TEST_DATABASE_URL and TEST_REDIS_URL are set. Example:

    docker run -d --rm --name he011-pg -e POSTGRES_PASSWORD=test -e POSTGRES_USER=hackevents \
        -e POSTGRES_DB=hackevents -p 55433:5432 postgres:16-alpine
    docker run -d --rm --name he011-redis -p 56380:6379 redis:7-alpine
    TEST_DATABASE_URL=postgresql+asyncpg://hackevents:test@localhost:55433/hackevents \
    TEST_REDIS_URL=redis://localhost:56380/0 uv run pytest services/tests/test_auth.py
"""

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import redis
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from api.auth import (
    SESSION_COOKIE,
    SESSION_TTL,
    check_password_policy,
    hash_password,
    hash_token,
)
from api.errors import ApiError
from api.main import app
from common.config import get_settings

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
GOOD_PASSWORD = "correct horse battery staple"

# --- static (no DB) ---


@pytest.mark.parametrize(
    "password",
    ["short", "elevenchars", "x" * 129, "password1234", "PassWord1234", "1q2w3e4r5t6y"],
)
def test_password_policy_rejects(password: str) -> None:
    with pytest.raises(ApiError) as exc:
        check_password_policy(password)
    assert exc.value.status == 422
    assert exc.value.code == "weak_password"


def test_password_policy_accepts_long_uncommon_password() -> None:
    check_password_policy(GOOD_PASSWORD)
    check_password_policy("twelve chars")


def test_hash_is_argon2id_and_salted() -> None:
    a, b = hash_password(GOOD_PASSWORD), hash_password(GOOD_PASSWORD)
    assert a.startswith("$argon2id$")
    assert a != b


def test_token_hash_is_sha256_hex() -> None:
    assert hash_token("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


# --- endpoints against real Postgres + Redis ---

DB_URL = os.environ.get("TEST_DATABASE_URL")
REDIS_URL = os.environ.get("TEST_REDIS_URL")
needs_stack = pytest.mark.skipif(
    not (DB_URL and REDIS_URL), reason="TEST_DATABASE_URL / TEST_REDIS_URL not set"
)


def sql(statement: str, **params: Any) -> list[Any]:
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


def make_user(email: str, password: str = GOOD_PASSWORD, active: bool = True) -> str:
    rows = sql(
        "INSERT INTO users (email, name, role, password_hash, is_active)"
        " VALUES (:e, :n, 'professor', :h, :a) RETURNING id",
        e=email,
        n=email.split("@")[0],
        h=hash_password(password),
        a=active,
    )
    return str(rows[0][0])


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
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
    # https base URL: the cookie is Secure, so the test client only sends it over https.
    with TestClient(app, base_url="https://testserver") as c:
        yield c
    get_settings.cache_clear()


def login(c: TestClient, email: str, password: str = GOOD_PASSWORD) -> Any:
    return c.post("/api/v1/auth/login", json={"email": email, "password": password})


def token_of(c: TestClient, email: str) -> str:
    r = login(c, email)
    assert r.status_code == 200, r.text
    token = c.cookies[SESSION_COOKIE]
    c.cookies.clear()
    return token


def as_token(token: str) -> dict[str, str]:
    return {"Cookie": f"{SESSION_COOKIE}={token}"}


@needs_stack
def test_login_ok_sets_hardened_cookie_and_stores_only_hash(client: TestClient) -> None:
    uid = make_user("prof@example.com")
    r = login(client, "Prof@Example.com")
    assert r.status_code == 200
    assert r.json() == {
        "id": uid,
        "email": "prof@example.com",
        "name": "prof",
        "role": "professor",
        "timezone": "Asia/Kolkata",
    }
    cookie = r.headers["set-cookie"].lower()
    for flag in ("httponly", "secure", "samesite=lax", "path=/", f"max-age={14 * 86400}"):
        assert flag in cookie
    token = client.cookies[SESSION_COOKIE]
    assert len(token) >= 43  # 32 random bytes, urlsafe base64
    stored = sql("SELECT token_hash FROM sessions")
    assert stored == [(hash_token(token),)]
    assert client.get("/api/v1/me").json()["id"] == uid


@needs_stack
def test_wrong_password_and_unknown_email_get_identical_response(client: TestClient) -> None:
    make_user("prof@example.com")
    make_user("gone@example.com", active=False)
    responses = [
        login(client, "prof@example.com", "wrong password here"),
        login(client, "nobody@example.com", "wrong password here"),
        login(client, "gone@example.com"),  # deactivated, correct password
    ]
    for r in responses:
        assert r.status_code == 401
        assert SESSION_COOKIE not in r.headers.get("set-cookie", "")
    assert len({r.text for r in responses}) == 1
    assert responses[0].json() == {
        "error": {
            "code": "invalid_credentials",
            "message": "Invalid email or password.",
            "details": {},
        }
    }
    assert sql("SELECT count(*) FROM sessions") == [(0,)]


@needs_stack
def test_sixth_attempt_per_email_is_rate_limited_even_with_right_password(
    client: TestClient,
) -> None:
    make_user("prof@example.com")
    for _ in range(5):
        assert login(client, "prof@example.com", "wrong password here").status_code == 401
    r = login(client, "PROF@example.com")  # case variant hits the same bucket
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"
    assert 0 < int(r.headers["retry-after"]) <= 15 * 60


@needs_stack
def test_sixth_attempt_per_ip_is_rate_limited_across_emails(client: TestClient) -> None:
    for i in range(5):
        assert login(client, f"u{i}@example.com", "wrong password here").status_code == 401
    assert login(client, "u9@example.com", "wrong password here").status_code == 429


@needs_stack
def test_successful_logins_do_not_consume_the_limit(client: TestClient) -> None:
    make_user("prof@example.com")
    for _ in range(7):
        assert login(client, "prof@example.com").status_code == 200


@needs_stack
def test_logout_revokes_session(client: TestClient) -> None:
    make_user("prof@example.com")
    token = token_of(client, "prof@example.com")
    r = client.post("/api/v1/auth/logout", headers=as_token(token))
    assert r.status_code == 204
    assert "max-age=0" in r.headers["set-cookie"].lower()
    assert sql("SELECT count(*) FROM sessions") == [(0,)]
    assert client.get("/api/v1/me", headers=as_token(token)).status_code == 401
    assert client.post("/api/v1/auth/logout", headers=as_token(token)).status_code == 401


@needs_stack
def test_unauthenticated_and_forged_tokens_rejected(client: TestClient) -> None:
    assert client.get("/api/v1/me").status_code == 401
    r = client.get("/api/v1/me", headers=as_token("forged-token"))
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"


@needs_stack
def test_expired_session_rejected(client: TestClient) -> None:
    make_user("prof@example.com")
    token = token_of(client, "prof@example.com")
    sql("UPDATE sessions SET expires_at = now() - interval '1 second'")
    assert client.get("/api/v1/me", headers=as_token(token)).status_code == 401


@needs_stack
def test_deactivated_user_session_rejected(client: TestClient) -> None:
    make_user("prof@example.com")
    token = token_of(client, "prof@example.com")
    sql("UPDATE users SET is_active = false")
    assert client.get("/api/v1/me", headers=as_token(token)).status_code == 401


@needs_stack
def test_sliding_expiry_refreshes_session_and_cookie(client: TestClient) -> None:
    make_user("prof@example.com")
    token = token_of(client, "prof@example.com")

    fresh = client.get("/api/v1/me", headers=as_token(token))
    assert fresh.status_code == 200
    assert "set-cookie" not in fresh.headers  # just issued: no write per request

    sql("UPDATE sessions SET expires_at = now() + interval '1 day'")
    r = client.get("/api/v1/me", headers=as_token(token))
    assert r.status_code == 200
    assert f"max-age={14 * 86400}" in r.headers["set-cookie"].lower()
    (remaining,) = sql("SELECT expires_at - now() FROM sessions")[0]
    assert remaining > SESSION_TTL - SESSION_TTL / 14 / 24  # ~14 days again


@needs_stack
def test_one_users_session_cannot_act_as_another(client: TestClient) -> None:
    a = make_user("a@example.com")
    b = make_user("b@example.com")
    token_a = token_of(client, "a@example.com")
    token_b = token_of(client, "b@example.com")
    assert client.get("/api/v1/me", headers=as_token(token_a)).json()["id"] == a
    assert client.get("/api/v1/me", headers=as_token(token_b)).json()["id"] == b

    # A logging out ends only A's session.
    assert client.post("/api/v1/auth/logout", headers=as_token(token_a)).status_code == 204
    assert client.get("/api/v1/me", headers=as_token(token_a)).status_code == 401
    assert client.get("/api/v1/me", headers=as_token(token_b)).json()["id"] == b
    assert sql("SELECT user_id::text FROM sessions") == [(b,)]
