"""HE-015: CSRF middleware. Runs without Postgres/Redis: rejected writes never reach a route,
and the accepting path is exercised on a tiny app with the same middleware. The full
login -> write -> logout flow against the real stack is in test_auth.py."""

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.csrf import CSRF_COOKIE, CSRF_HEADER, CSRFMiddleware, csrf_for
from api.main import app

SESSION = "__Host-session"
real = TestClient(app, base_url="https://testserver")  # no lifespan: no DB needed

tiny_app = FastAPI()
tiny_app.add_middleware(CSRFMiddleware, session_cookie=SESSION)


@tiny_app.api_route("/w", methods=["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"])
def w() -> dict[str, bool]:
    return {"ok": True}


tiny = TestClient(tiny_app, base_url="https://testserver")


def pair(token: str) -> dict[str, str]:
    return {"Cookie": f"{CSRF_COOKIE}={token}", CSRF_HEADER: token}


def assert_csrf_failed(r: Any) -> None:
    assert r.status_code == 403
    body = r.json()
    assert body["error"]["code"] == "csrf_failed"
    assert set(body["error"]) == {"code", "message", "details"}


def test_real_app_writes_without_token_get_403() -> None:
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert_csrf_failed(real.request(method, "/api/v1/auth/logout"))
        assert_csrf_failed(real.request(method, "/api/v1/does-not-exist"))  # deny by default
    assert_csrf_failed(real.post("/api/v1/auth/login", json={"email": "a@b.c", "password": "x"}))


def test_real_app_reads_unaffected() -> None:
    assert real.get("/api/v1/health/live").status_code == 200
    assert real.head("/api/v1/health/live").status_code != 403


def test_token_issue_endpoint_needs_no_stack() -> None:
    c = TestClient(app, base_url="https://testserver")
    r = c.get("/api/v1/auth/csrf")
    assert r.status_code == 200
    token = r.json()["csrf_token"]
    assert len(token) >= 43 and c.cookies[CSRF_COOKIE] == token
    assert c.get("/api/v1/auth/csrf").json()["csrf_token"] == token
    r = c.get("/api/v1/auth/csrf", headers={"Cookie": f"{SESSION}=s3ssion"})
    assert r.json()["csrf_token"] == csrf_for("s3ssion")


def test_matching_pair_passes_every_unsafe_method() -> None:
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert tiny.request(method, "/w", headers=pair("abc")).status_code == 200
    for method in ("GET", "HEAD", "OPTIONS"):
        assert tiny.request(method, "/w").status_code == 200


def test_missing_or_mismatched_token_rejected() -> None:
    assert_csrf_failed(tiny.post("/w"))
    assert_csrf_failed(tiny.post("/w", headers={CSRF_HEADER: "abc"}))  # no cookie
    assert_csrf_failed(tiny.post("/w", headers={"Cookie": f"{CSRF_COOKIE}=abc"}))  # no header
    assert_csrf_failed(tiny.post("/w", headers={**pair("abc"), CSRF_HEADER: "abd"}))
    assert_csrf_failed(tiny.post("/w", headers={**pair(""), CSRF_HEADER: ""}))
    # Non-ASCII (latin-1) header bytes must fail closed, not crash compare_digest.
    latin1 = [(b"cookie", f"{CSRF_COOKIE}=abc".encode()), (CSRF_HEADER.encode(), b"ab\xe9")]
    assert_csrf_failed(tiny.post("/w", headers=latin1))


def test_with_session_only_the_session_bound_token_passes() -> None:
    bound = csrf_for("s3ssion")
    cookie = f"{SESSION}=s3ssion; {CSRF_COOKIE}="
    ok = {"Cookie": cookie + bound, CSRF_HEADER: bound}
    assert tiny.post("/w", headers=ok).status_code == 200
    # A self-consistent pair that was not derived from this session is a tossed cookie.
    assert_csrf_failed(tiny.post("/w", headers={"Cookie": cookie + "abc", CSRF_HEADER: "abc"}))
    assert_csrf_failed(tiny.post("/w", headers={**ok, "Cookie": cookie + "abc"}))


def test_origin_must_be_app_domain_when_present() -> None:
    ok = pair("abc")
    assert tiny.post("/w", headers={**ok, "Origin": "https://localhost"}).status_code == 200
    for origin in ("https://evil.example", "http://localhost", "https://localhost.evil", "null"):
        assert_csrf_failed(tiny.post("/w", headers={**ok, "Origin": origin}))


def test_csrf_for_is_deterministic_and_session_specific() -> None:
    assert csrf_for("a") == csrf_for("a") != csrf_for("b")
    assert len(csrf_for("a")) == 43
