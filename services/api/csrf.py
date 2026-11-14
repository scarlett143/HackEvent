"""CSRF protection (HE-015, doc 07 §4, doc 06 conventions): session-bound double-submit token.

Every request whose method is not GET/HEAD/OPTIONS must carry header `X-CSRF-Token` equal to
the `__Host-csrf` cookie, else 403 `csrf_failed`. There is no exemption list (deny by default):
login and other pre-session writes fetch a token from `GET /api/v1/auth/csrf` first, which also
blocks login CSRF (signing a victim into an attacker's account).

- With a session cookie, the only valid token is `csrf_for(session token)`, an HMAC keyed by the
  secret session token. A token minted for any other session (or tossed in by another origin)
  never verifies, and it rotates on every login because the session token does.
- Without a session, the token is random; the `__Host-` prefix (Secure, Path=/, no Domain)
  stops sibling subdomains or plain-http origins from planting one.
- If an `Origin` header is present it must be exactly `https://APP_DOMAIN` (CORS: same origin).
"""

import base64
import hashlib
import hmac
import secrets

from fastapi import Response
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from api.errors import ApiError, error_response
from common.config import get_settings

CSRF_COOKIE = "__Host-csrf"  # readable by JS on purpose; holds no authority on its own
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def csrf_for(session_token: str) -> str:
    mac = hmac.digest(session_token.encode(), b"hackevents-csrf-v1", hashlib.sha256)
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def set_csrf_cookie(response: Response, value: str, max_age: int) -> None:
    response.set_cookie(
        CSRF_COOKIE, value, max_age=max_age, path="/", secure=True, httponly=False, samesite="lax"
    )


def clear_csrf_cookie(response: Response) -> None:
    response.delete_cookie(CSRF_COOKIE, path="/", secure=True, httponly=False, samesite="lax")


def new_csrf_token(session_token: str | None, current_cookie: str | None) -> str:
    """The token `CSRFMiddleware` will accept for this cookie state."""
    if session_token:
        return csrf_for(session_token)
    return current_cookie or secrets.token_urlsafe(32)


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


class CSRFMiddleware:
    def __init__(self, app: ASGIApp, session_cookie: str) -> None:
        self.app = app
        self.session_cookie = session_cookie

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            error = self._check(Request(scope))
            if error is not None:
                await error_response(error)(scope, receive, send)
                return
        await self.app(scope, receive, send)

    def _check(self, request: Request) -> ApiError | None:
        origin = request.headers.get("origin")
        if origin is not None and origin != f"https://{get_settings().app_domain}":
            return ApiError(403, "csrf_failed", "Cross-origin request blocked.")
        header = request.headers.get(CSRF_HEADER, "")
        cookie = request.cookies.get(CSRF_COOKIE, "")
        session = request.cookies.get(self.session_cookie)
        expected = csrf_for(session) if session else cookie
        if not expected or not (_same(header, expected) and _same(cookie, expected)):
            return ApiError(403, "csrf_failed", "Missing or invalid CSRF token. Reload the page.")
        return None
