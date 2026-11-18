"""Google OAuth 2.0 / OpenID Connect helpers (HE-012 sign-in; doc 07 §4, doc 06 A1).

Authorization-code flow with PKCE (S256). The ID token from the code exchange is verified
locally: RS256 signature against Google's JWKS (cached for JWKS_TTL_S, refetched once on an
unknown `kid`), `iss`, `aud` = our client id, `exp` / `iat` (60 s leeway), the `nonce` we
sent, and `email_verified`. PyJWT does the JWT maths (it was already locked, as a redis
dependency). Codes and tokens are never logged or put in exception messages.

Inert when GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are unset: `configured()` is False and
callers refuse to start a flow.
"""

import base64
import hashlib
import hmac
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx2
import jwt

from common.config import Settings, get_settings

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
TIMEOUT = httpx2.Timeout(15.0, connect=5.0)
JWKS_TTL_S = 3600
LEEWAY_S = 60

# kid -> key, and when to refetch. Module state: one cache per process.
_JWKS: dict[str, Any] = {"keys": {}, "expires": 0.0}


class GoogleError(Exception):
    """A Google OAuth step failed. The message carries an error code only, never tokens."""


def configured(settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    return bool(s.google_client_id and s.google_client_secret.get_secret_value())


def http_client() -> httpx2.AsyncClient:
    """Outbound client for Google; tests patch this with a MockTransport client."""
    return httpx2.AsyncClient(timeout=TIMEOUT)


def pkce() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge), RFC 7636."""
    verifier = secrets.token_urlsafe(64)  # 86 chars, within 43..128
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def auth_url(*, redirect_uri: str, scope: str, state: str, challenge: str, **extra: str) -> str:
    params = {
        "client_id": get_settings().google_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        **extra,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def error_code(resp: httpx2.Response) -> str:
    """`invalid_grant` etc. from an OAuth error body; never the body itself."""
    try:
        err = resp.json().get("error")
    except ValueError:
        return ""
    return err if isinstance(err, str) else ""


async def exchange_code(
    http: httpx2.AsyncClient, *, code: str, verifier: str, redirect_uri: str
) -> dict[str, Any]:
    s = get_settings()
    try:
        resp = await http.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
                "client_id": s.google_client_id,
                "client_secret": s.google_client_secret.get_secret_value(),
            },
        )
    except httpx2.TransportError as exc:
        raise GoogleError(f"token endpoint unreachable: {type(exc).__name__}") from None
    if resp.status_code != 200:
        raise GoogleError(f"code exchange failed: HTTP {resp.status_code} {error_code(resp)}")
    body: dict[str, Any] = resp.json()
    return body


async def _signing_key(http: httpx2.AsyncClient, kid: str) -> jwt.PyJWK:
    keys: dict[str, jwt.PyJWK] = _JWKS["keys"]
    if kid not in keys or time.monotonic() > _JWKS["expires"]:
        try:
            resp = await http.get(JWKS_URL)
        except httpx2.TransportError as exc:
            raise GoogleError(f"JWKS unreachable: {type(exc).__name__}") from None
        if resp.status_code != 200:
            raise GoogleError(f"JWKS fetch failed: HTTP {resp.status_code}")
        keys = {
            str(k["kid"]): jwt.PyJWK(k, algorithm="RS256")
            for k in resp.json().get("keys", [])
            if k.get("kty") == "RSA" and k.get("kid")
        }
        _JWKS.update(keys=keys, expires=time.monotonic() + JWKS_TTL_S)
    if kid not in keys:
        raise GoogleError("ID token signed by an unknown key")
    return keys[kid]


async def verify_id_token(http: httpx2.AsyncClient, id_token: str, nonce: str) -> dict[str, Any]:
    """Verified claims of a Google ID token, or GoogleError."""
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError:
        raise GoogleError("malformed ID token") from None
    if header.get("alg") != "RS256":
        raise GoogleError("unexpected ID token algorithm")
    key = await _signing_key(http, str(header.get("kid", "")))
    try:
        claims: dict[str, Any] = jwt.decode(
            id_token,
            key.key,
            algorithms=["RS256"],
            audience=get_settings().google_client_id,
            issuer=ISSUERS,
            leeway=LEEWAY_S,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise GoogleError(f"invalid ID token: {type(exc).__name__}") from None
    if not hmac.compare_digest(str(claims.get("nonce", "")).encode(), nonce.encode()):
        raise GoogleError("ID token nonce mismatch")
    if claims.get("email_verified") is not True or not claims.get("email"):
        raise GoogleError("Google email not verified")
    return claims
