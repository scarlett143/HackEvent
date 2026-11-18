"""HE-012: Google sign-in (OIDC) matched to the invited email. Google is faked with an
httpx2 MockTransport and a throwaway RSA key generated per run; no real credentials."""

import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from tests.stack import make_actor, needs_stack, sql

from api.auth import SESSION_COOKIE
from api.google import STATE_COOKIE, safe_next
from common import google
from common.config import get_settings
from common.logging import REDACTED, redact

CLIENT_ID = "test-client.apps.googleusercontent.com"
DOMAIN = "events.example.test"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    return {**RSAAlgorithm.to_jwk(key.public_key(), as_dict=True), "kid": kid, "alg": "RS256"}


def id_token(*, key: rsa.RSAPrivateKey = KEY, kid: str = "k1", **claims: Any) -> str:
    now = int(time.time())
    body = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "google-sub-1",
        "email": "a@example.com",
        "email_verified": True,
        "iat": now,
        "exp": now + 600,
        "nonce": "n",
        **claims,
    }
    return jwt.encode(body, key, algorithm="RS256", headers={"kid": kid})


class FakeGoogle:
    """Token endpoint returns `self.claims` signed as an ID token; JWKS serves key k1."""

    def __init__(self) -> None:
        self.claims: dict[str, Any] = {}
        self.token_status = 200
        self.nonce = ""  # set from the start redirect, as Google echoes it
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == google.JWKS_URL:
            return httpx2.Response(200, json={"keys": [_jwk(KEY, "k1")]})
        assert url == google.TOKEN_URL
        if self.token_status != 200:
            return httpx2.Response(self.token_status, json={"error": "invalid_grant"})
        return httpx2.Response(200, json={"id_token": id_token(nonce=self.nonce, **self.claims)})

    def calls(self, url: str) -> list[httpx2.Request]:
        return [r for r in self.requests if str(r.url) == url]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGoogle]:
    g = FakeGoogle()
    monkeypatch.setattr(
        google, "http_client", lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(g))
    )
    google._JWKS.update(keys={}, expires=0.0)
    yield g
    google._JWKS.update(keys={}, expires=0.0)


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "fake-client-secret")
    monkeypatch.setenv("APP_DOMAIN", DOMAIN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --- ID token verification (no DB) ---


async def _verify(fake: FakeGoogle, token: str, nonce: str = "n") -> dict[str, Any]:
    async with google.http_client() as http:
        return await google.verify_id_token(http, token, nonce)


async def test_valid_id_token_verifies_and_jwks_is_cached(
    configured: None, fake: FakeGoogle
) -> None:
    claims = await _verify(fake, id_token())
    assert claims["email"] == "a@example.com"
    await _verify(fake, id_token())
    assert len(fake.calls(google.JWKS_URL)) == 1  # cached


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(lambda: id_token(aud="someone-else"), id="wrong-audience"),
        pytest.param(lambda: id_token(iss="https://evil.example"), id="wrong-issuer"),
        pytest.param(lambda: id_token(exp=int(time.time()) - 3600), id="expired"),
        pytest.param(lambda: id_token(nonce="other"), id="wrong-nonce"),
        pytest.param(lambda: id_token(email_verified=False), id="email-unverified"),
        pytest.param(lambda: id_token(key=OTHER_KEY), id="bad-signature"),
        pytest.param(lambda: id_token(kid="unknown"), id="unknown-kid"),
        pytest.param(lambda: "not.a.jwt", id="malformed"),
        pytest.param(
            lambda: jwt.encode({"sub": "x"}, "k" * 32, algorithm="HS256"), id="hs256-confusion"
        ),
    ],
)
async def test_bad_id_tokens_are_rejected(configured: None, fake: FakeGoogle, token: Any) -> None:
    with pytest.raises(google.GoogleError):
        await _verify(fake, token())


async def test_unknown_kid_refetches_jwks_once(configured: None, fake: FakeGoogle) -> None:
    await _verify(fake, id_token())
    with pytest.raises(google.GoogleError):
        await _verify(fake, id_token(kid="rotated"))
    assert len(fake.calls(google.JWKS_URL)) == 2


def test_pkce_challenge_is_s256_of_verifier() -> None:
    import base64
    import hashlib

    verifier, challenge = google.pkce()
    assert 43 <= len(verifier) <= 128
    digest = hashlib.sha256(verifier.encode()).digest()
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def test_safe_next_blocks_open_redirects() -> None:
    assert safe_next("/events/1?x=2") == "/events/1?x=2"
    for bad in (None, "", "https://evil.example", "//evil.example", "/\\evil.example"):
        assert safe_next(bad) == "/"


def test_callback_code_is_redacted_from_logs() -> None:
    line = '"GET /api/v1/auth/google/callback?state=abc123&code=4/0AbCdEf-xyz&scope=email" 302'
    out = redact(line)
    assert "4/0AbCdEf-xyz" not in out
    assert f"state=abc123&code={REDACTED}&scope=email" in out


# --- endpoints against real Postgres + Redis ---


def _start(client: TestClient, fake: FakeGoogle, next_: str = "/") -> dict[str, list[str]]:
    r = client.get("/api/v1/auth/google/start", params={"next": next_}, follow_redirects=False)
    assert r.status_code == 302, r.text
    query = parse_qs(urlsplit(r.headers["location"]).query)
    fake.nonce = query["nonce"][0]
    return query


def _callback(client: TestClient, state: str, **params: str) -> httpx2.Response:
    return client.get(
        "/api/v1/auth/google/callback",
        params={"state": state, "code": "fake-code", **params},
        follow_redirects=False,
    )


@needs_stack
def test_unconfigured_google_is_hidden_and_inert(stack_client: TestClient) -> None:
    assert stack_client.get("/api/v1/auth/providers").json() == {"google": False}
    r = stack_client.get("/api/v1/auth/google/start", follow_redirects=False)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "google_not_configured"


@needs_stack
def test_start_redirects_to_google_with_state_nonce_and_pkce(
    stack_client: TestClient, configured: None, fake: FakeGoogle
) -> None:
    assert stack_client.get("/api/v1/auth/providers").json() == {"google": True}
    q = _start(stack_client, fake)
    assert q["client_id"] == [CLIENT_ID]
    assert q["redirect_uri"] == [f"https://{DOMAIN}/api/v1/auth/google/callback"]
    assert q["response_type"] == ["code"] and q["scope"] == ["openid email"]
    assert q["code_challenge_method"] == ["S256"] and q["code_challenge"][0]
    assert len(q["state"][0]) >= 40 and len(q["nonce"][0]) >= 40
    assert stack_client.cookies.get(STATE_COOKIE) == q["state"][0]


@needs_stack
def test_matching_email_signs_in_and_binds_google_sub(
    stack_client: TestClient, configured: None, fake: FakeGoogle
) -> None:
    a = make_actor("a@example.com")
    q = _start(stack_client, fake, next_="/events")
    fake.claims = {"email": "A@Example.com"}
    r = _callback(stack_client, q["state"][0])
    assert r.status_code == 302 and r.headers["location"] == "/events"
    assert SESSION_COOKIE in r.headers.get("set-cookie", "")
    # the code exchange sent the PKCE verifier and our redirect URI
    form = parse_qs(fake.calls(google.TOKEN_URL)[0].content.decode())
    assert form["redirect_uri"] == [f"https://{DOMAIN}/api/v1/auth/google/callback"]
    assert len(form["code_verifier"][0]) >= 43
    me = stack_client.get("/api/v1/me")
    assert me.status_code == 200 and me.json()["id"] == a.id
    assert sql("SELECT google_sub FROM users WHERE email = 'a@example.com'") == [("google-sub-1",)]
    assert sql("SELECT meta FROM audit_log WHERE action = 'auth.login_success'") == [
        ({"method": "google"},)
    ]


@needs_stack
def test_wrong_email_is_rejected(
    stack_client: TestClient, configured: None, fake: FakeGoogle
) -> None:
    make_actor("a@example.com")
    q = _start(stack_client, fake)
    fake.claims = {"email": "stranger@example.com"}
    r = _callback(stack_client, q["state"][0])
    assert r.status_code == 302 and r.headers["location"] == "/login?error=google_no_account"
    assert SESSION_COOKIE not in r.headers.get("set-cookie", "")
    assert stack_client.get("/api/v1/me").status_code == 401
    assert sql("SELECT count(*) FROM users") == [(1,)]  # no self sign-up
    assert sql("SELECT meta FROM audit_log WHERE action = 'auth.login_fail'") == [
        ({"method": "google", "reason": "no_account"},)
    ]


@needs_stack
@pytest.mark.parametrize(
    ("setup", "claims"),
    [
        ("UPDATE users SET is_active = false", {}),  # deactivated
        ("UPDATE users SET google_sub = 'another-google-account'", {}),  # bound elsewhere
        ("SELECT 1", {"email_verified": False}),
        ("SELECT 1", {"aud": "other-client"}),
    ],
)
def test_other_rejections(
    stack_client: TestClient,
    configured: None,
    fake: FakeGoogle,
    setup: str,
    claims: dict[str, Any],
) -> None:
    make_actor("a@example.com")
    sql(setup)
    q = _start(stack_client, fake)
    fake.claims = dict(claims)
    r = _callback(stack_client, q["state"][0])
    assert r.status_code == 302 and r.headers["location"].startswith("/login?error=google_")
    assert stack_client.get("/api/v1/me").status_code == 401


@needs_stack
def test_state_must_match_cookie_and_is_single_use(
    stack_client: TestClient, configured: None, fake: FakeGoogle
) -> None:
    make_actor("a@example.com")
    state = _start(stack_client, fake)["state"][0]
    stack_client.cookies.clear()  # from here on each request carries exactly the cookie given

    def as_other(cookie: str | None, state_: str) -> str:
        headers = {"Cookie": f"{STATE_COOKIE}={cookie}"} if cookie else {}
        r = stack_client.get(
            "/api/v1/auth/google/callback",
            params={"state": state_, "code": "fake-code"},
            headers=headers,
            follow_redirects=False,
        )
        stack_client.cookies.clear()
        return str(r.headers["location"])

    # no / wrong state cookie (login CSRF: attacker's flow in the victim's browser) -> refused
    assert as_other(None, state) == "/login?error=google_state"
    assert as_other("something-else", state) == "/login?error=google_state"
    assert as_other("forged", "forged") == "/login?error=google_state"  # unknown to Redis
    assert fake.calls(google.TOKEN_URL) == []  # never exchanged
    # the real one works once, then it's consumed
    assert as_other(state, state) == "/"
    assert as_other(state, state) == "/login?error=google_state"


@needs_stack
def test_user_cancel_and_token_errors_redirect_to_login(
    stack_client: TestClient, configured: None, fake: FakeGoogle
) -> None:
    make_actor("a@example.com")
    q = _start(stack_client, fake)
    r = _callback(stack_client, q["state"][0], error="access_denied")
    assert r.headers["location"] == "/login?error=google_cancelled"
    q = _start(stack_client, fake)
    fake.token_status = 400
    r = _callback(stack_client, q["state"][0])
    assert r.headers["location"] == "/login?error=google_failed"
    assert sql("SELECT meta FROM audit_log") == [({"method": "google", "reason": "failed"},)]
