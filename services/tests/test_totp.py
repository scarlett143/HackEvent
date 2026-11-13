"""HE-014: admin TOTP 2FA. Admin can't reach /admin without 2FA.

RFC 6238 tests run everywhere. Endpoint tests need the same THROWAWAY Postgres + Redis as
test_auth.py (TEST_DATABASE_URL + TEST_REDIS_URL) and use a random test MASTER_KEY.
"""

import base64
import os
from collections.abc import Iterator
from typing import Any

import pytest
import redis
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from tests.test_auth import ALEMBIC_INI, DB_URL, GOOD_PASSWORD, REDIS_URL, needs_stack, sql

from api import totp
from api.auth import SESSION_COOKIE, hash_password
from api.main import app
from common import crypto
from common.config import get_settings

ADMIN = "/api/v1/admin/ping"

# --- RFC 6238 (no stack) ---

RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()  # RFC 6238 App. B, SHA1


@pytest.mark.parametrize(
    ("unix", "expected"),
    [(59, "287082"), (1111111109, "081804"), (1234567890, "005924"), (2000000000, "279037")],
)
def test_rfc6238_vectors(unix: int, expected: str) -> None:
    # Appendix B lists 8 digits; the 6-digit code is the low 6 digits of the same value.
    assert totp.code_at(RFC_SECRET, totp.current_step(unix)) == expected


def test_window_is_plus_minus_one_step() -> None:
    now = 1_700_000_000.0
    step = totp.current_step(now)
    for delta in (-1, 0, 1):
        assert totp.match_step(RFC_SECRET, totp.code_at(RFC_SECRET, step + delta), now) == (
            step + delta
        )
    for delta in (-2, 2):
        code = totp.code_at(RFC_SECRET, step + delta)
        if code not in {totp.code_at(RFC_SECRET, step + d) for d in (-1, 0, 1)}:
            assert totp.match_step(RFC_SECRET, code, now) is None
    assert totp.match_step(RFC_SECRET, "abcdef", now) is None
    assert totp.match_step(RFC_SECRET, "12345", now) is None


def test_provisioning_uri_and_secret_shape() -> None:
    secret = totp.new_secret()
    assert len(secret) == 32 and secret.isalnum()  # 160 bits, base32, no padding
    uri = totp.provisioning_uri(secret, "admin@example.com")
    assert uri.startswith("otpauth://totp/HackEvents%3Aadmin%40example.com?")
    assert f"secret={secret}" in uri and "period=30" in uri and "digits=6" in uri


# --- endpoints ---


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    assert DB_URL and REDIS_URL
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("MASTER_KEY", base64.b64encode(os.urandom(32)).decode())
    get_settings.cache_clear()
    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["skip_logging"] = True
    cfg.attributes["database_url"] = DB_URL
    command.upgrade(cfg, "head")
    sql("TRUNCATE users CASCADE")
    redis.Redis.from_url(REDIS_URL).flushdb()
    with TestClient(app, base_url="https://testserver") as c:
        yield c
    get_settings.cache_clear()


def make(email: str, role: str = "admin") -> str:
    rows = sql(
        "INSERT INTO users (email, name, role, password_hash)"
        " VALUES (:e, 'x', CAST(:r AS user_role), :h) RETURNING id",
        e=email,
        r=role,
        h=hash_password(GOOD_PASSWORD),
    )
    return str(rows[0][0])


def session(c: TestClient, email: str, **extra: Any) -> dict[str, str]:
    """Log in and return headers for that session (cookie jar left empty)."""
    r = c.post("/api/v1/auth/login", json={"email": email, "password": GOOD_PASSWORD, **extra})
    assert r.status_code == 200, r.text
    token = c.cookies[SESSION_COOKIE]
    c.cookies.clear()
    return {"Cookie": f"{SESSION_COOKIE}={token}"}


def code(secret: str, delta: int = 0) -> str:
    return totp.code_at(secret, totp.current_step() + delta)


def wrong(secret: str) -> str:
    valid = {code(secret, d) for d in range(-2, 3)}
    return next(c for c in (f"{i:06d}" for i in range(10**6)) if c not in valid)


def enrolled(c: TestClient, email: str) -> tuple[dict[str, str], str, int]:
    """Admin enrols + confirms; returns (verified session headers, secret, consumed step)."""
    h = session(c, email)
    secret = c.post("/api/v1/auth/2fa/enrol", headers=h).json()["secret"]
    step = totp.current_step()
    r = c.post("/api/v1/auth/2fa/confirm", headers=h, json={"code": totp.code_at(secret, step)})
    assert r.status_code == 200, r.text
    return h, secret, step


def after(secret: str, step: int) -> str:
    """A code newer than `step` that is still inside the server's window."""
    return totp.code_at(secret, step + 1)


def err(r: Any) -> tuple[int, str]:
    return r.status_code, r.json()["error"]["code"]


@needs_stack
def test_admin_without_2fa_is_blocked_then_enrol_unlocks(client: TestClient) -> None:
    uid = make("admin@example.com")
    h = session(client, "admin@example.com")
    assert err(client.get(ADMIN, headers=h)) == (403, "totp_enrolment_required")

    r = client.post("/api/v1/auth/2fa/enrol", headers=h)
    assert r.status_code == 200
    secret = r.json()["secret"]
    assert r.json()["otpauth_uri"].startswith("otpauth://totp/")
    assert err(client.get(ADMIN, headers=h)) == (403, "totp_enrolment_required")  # pending

    # Stored encrypted, bound to the row; plaintext never in the DB.
    (blob,) = sql("SELECT totp_secret_enc FROM users WHERE id = :i", i=uid)[0]
    assert secret.encode() not in bytes(blob)
    aad = crypto.aad("users", "totp_secret_enc", uid)
    assert crypto.decrypt(bytes(blob), aad).decode() == secret

    r = client.post("/api/v1/auth/2fa/confirm", headers=h, json={"code": code(secret)})
    assert r.status_code == 200
    assert client.get(ADMIN, headers=h).json() == {"status": "ok"}
    assert err(client.post("/api/v1/auth/2fa/enrol", headers=h)) == (409, "totp_already_enabled")


@needs_stack
def test_new_session_needs_verify_and_rejects_wrong_and_replayed_codes(
    client: TestClient,
) -> None:
    make("admin@example.com")
    _, secret, step = enrolled(client, "admin@example.com")

    h = session(client, "admin@example.com")
    assert err(client.get(ADMIN, headers=h)) == (403, "totp_required")
    bad = {"code": wrong(secret)}
    assert err(client.post("/api/v1/auth/2fa/verify", headers=h, json=bad)) == (400, "invalid_totp")
    replay = {"code": totp.code_at(secret, step)}  # the code the confirm used
    assert err(client.post("/api/v1/auth/2fa/verify", headers=h, json=replay)) == (
        400,
        "invalid_totp",
    )
    assert err(client.get(ADMIN, headers=h)) == (403, "totp_required")

    nxt = {"code": after(secret, step)}
    assert client.post("/api/v1/auth/2fa/verify", headers=h, json=nxt).status_code == 200
    assert client.get(ADMIN, headers=h).status_code == 200
    # The same next-step code cannot verify a second session either.
    h2 = session(client, "admin@example.com")
    assert err(client.post("/api/v1/auth/2fa/verify", headers=h2, json=nxt))[0] == 400


@needs_stack
def test_login_with_totp_verifies_in_one_step(client: TestClient) -> None:
    make("admin@example.com")
    _, secret, step = enrolled(client, "admin@example.com")
    r = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": GOOD_PASSWORD, "totp": wrong(secret)},
    )
    assert err(r) == (400, "invalid_totp")
    assert SESSION_COOKIE not in r.headers.get("set-cookie", "")
    h = session(client, "admin@example.com", totp=after(secret, step))
    assert client.get(ADMIN, headers=h).status_code == 200


@needs_stack
def test_code_attempts_rate_limited(client: TestClient) -> None:
    make("admin@example.com")
    _, secret, step = enrolled(client, "admin@example.com")
    h = session(client, "admin@example.com")
    for _ in range(5):
        r = client.post("/api/v1/auth/2fa/verify", headers=h, json={"code": wrong(secret)})
        assert r.status_code == 400
    r = client.post("/api/v1/auth/2fa/verify", headers=h, json={"code": after(secret, step)})
    assert err(r) == (429, "rate_limited")
    assert int(r.headers["retry-after"]) > 0
    assert err(client.get(ADMIN, headers=h)) == (403, "totp_required")


@needs_stack
def test_non_admin_never_reaches_admin_or_2fa_routes(client: TestClient) -> None:
    for role in ("management", "professor"):
        make(f"{role}@example.com", role)
        h = session(client, f"{role}@example.com")
        assert err(client.get(ADMIN, headers=h)) == (403, "forbidden")
        assert err(client.post("/api/v1/auth/2fa/enrol", headers=h)) == (403, "forbidden")
        r = client.post("/api/v1/auth/2fa/verify", headers=h, json={"code": "123456"})
        assert err(r) == (403, "forbidden")
    assert err(client.get(ADMIN)) == (401, "unauthenticated")


@needs_stack
def test_one_admins_code_cannot_verify_another_admins_session(client: TestClient) -> None:
    make("a@example.com")
    make("b@example.com")
    _, secret_a, step_a = enrolled(client, "a@example.com")
    hb_verified, secret_b, _ = enrolled(client, "b@example.com")

    hb = session(client, "b@example.com")
    code_a = after(secret_a, step_a)  # valid for A right now
    r = client.post("/api/v1/auth/2fa/verify", headers=hb, json={"code": code_a})
    if code_a not in {code(secret_b, d) for d in (-1, 0, 1)}:
        assert err(r) == (400, "invalid_totp")
    assert err(client.get(ADMIN, headers=hb)) == (403, "totp_required")
    # A's verified session leaves B's other sessions untouched, and vice versa.
    ha = session(client, "a@example.com")
    assert err(client.get(ADMIN, headers=ha)) == (403, "totp_required")
    assert client.get(ADMIN, headers=hb_verified).status_code == 200


@needs_stack
def test_totp_fails_clearly_without_master_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    make("admin@example.com")
    h = session(client, "admin@example.com")
    monkeypatch.delenv("MASTER_KEY")
    get_settings.cache_clear()
    assert err(client.post("/api/v1/auth/2fa/enrol", headers=h)) == (503, "totp_unavailable")
    assert sql("SELECT totp_secret_enc FROM users") == [(None,)]
