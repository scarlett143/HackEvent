"""Google sign-in (HE-012, doc 07 §4 "Optional Google sign-in (OIDC), must match the invited
email", doc 06 A1).

- `GET /auth/providers` (public): `{"google": bool}`, so the login page shows the button
  only when GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are set.
- `GET /auth/google/start?next=/path` (public): 302 to Google with `state`, `nonce` and a
  PKCE S256 challenge. state -> {nonce, verifier, next} lives in Redis for 10 min (single
  use) and the state is also put in an HttpOnly `__Host-` cookie, so a callback only
  completes in the browser that started it (blocks login CSRF). Unconfigured -> 404.
- `GET /auth/google/callback` (public): cookie == state, Redis entry consumed, code
  exchanged with the PKCE verifier, ID token verified (common.google). Then sign-in only
  for an existing ACTIVE user whose email equals the verified Google email (invites create
  users; there is no self sign-up). The first sign-in binds `users.google_sub`; after that
  the same email from a different Google account is rejected. Success -> new session (as
  password login: admins still need TOTP for /admin) and 302 to `next`; any failure -> 302
  to `/login?error=google_<reason>`. Both outcomes are audited (doc 07 §11 login success/fail).

The redirect URI is built only from APP_DOMAIN. Codes, states and tokens are never logged
(the log filter redacts `code=` / `state=` query values).
"""

import hashlib
import hmac
import json
import logging
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import start_session
from api.deps import get_db, get_redis
from api.errors import ApiError
from api.invites import _ip
from common import google
from common.audit import AuditAction, record
from common.config import get_settings
from common.models import User

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")

Db = Annotated[AsyncSession, Depends(get_db)]
Rds = Annotated[Redis, Depends(get_redis)]

STATE_COOKIE = "__Host-oauth-state"
STATE_TTL_S = 600
LOGIN_CALLBACK = "/api/v1/auth/google/callback"


class Rejected(Exception):
    """OAuth flow refused; `args[0]` is a short reason code for the redirect."""


def callback_uri(path: str) -> str:
    return f"https://{get_settings().app_domain}{path}"


def safe_next(next_: str | None) -> str:
    """Same-site absolute paths only (same rule as the web `safeNext`): no open redirect."""
    if next_ and next_.startswith("/") and not next_.startswith(("//", "/\\")):
        return next_
    return "/"


def _state_key(state: str) -> str:
    return f"oauth:google:{hashlib.sha256(state.encode()).hexdigest()}"


async def begin(
    redis: Redis, *, purpose: str, callback: str, scope: str, data: dict[str, str], **extra: str
) -> RedirectResponse:
    """Start an authorization-code + PKCE flow: remember it, set the state cookie, 302."""
    if not google.configured():
        raise ApiError(404, "google_not_configured", "Google sign-in is not set up here.")
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    verifier, challenge = google.pkce()
    entry = {"purpose": purpose, "nonce": nonce, "verifier": verifier, **data}
    await redis.set(_state_key(state), json.dumps(entry), ex=STATE_TTL_S)
    url = google.auth_url(
        redirect_uri=callback_uri(callback),
        scope=scope,
        state=state,
        challenge=challenge,
        nonce=nonce,
        **extra,
    )
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(
        STATE_COOKIE,
        state,
        max_age=STATE_TTL_S,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return resp


async def finish(
    request: Request,
    redis: Redis,
    *,
    purpose: str,
    callback: str,
    code: str | None,
    state: str | None,
    error: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Check state (cookie + single-use Redis entry), exchange the code.
    Returns (flow data, token response); raises Rejected or google.GoogleError."""
    cookie = request.cookies.get(STATE_COOKIE, "")
    if not state or not cookie or not hmac.compare_digest(cookie.encode(), state.encode()):
        raise Rejected("state")
    raw = await redis.getdel(_state_key(state))
    if raw is None:
        raise Rejected("state")
    data: dict[str, Any] = json.loads(raw)
    if data.get("purpose") != purpose:
        raise Rejected("state")
    if error or not code:
        raise Rejected("cancelled")
    async with google.http_client() as http:
        tokens = await google.exchange_code(
            http, code=code, verifier=data["verifier"], redirect_uri=callback_uri(callback)
        )
    return data, tokens


def back(path: str) -> RedirectResponse:
    resp = RedirectResponse(path, status_code=302)
    resp.delete_cookie(STATE_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return resp


class ProvidersOut(BaseModel):
    google: bool


@router.get("/auth/providers")
def providers() -> ProvidersOut:  # role: public; which sign-in buttons to show
    return ProvidersOut(google=google.configured())


@router.get("/auth/google/start")
async def google_start(
    redis: Rds, next: str | None = None
) -> RedirectResponse:  # role: public; starts sign-in
    return await begin(
        redis,
        purpose="login",
        callback=LOGIN_CALLBACK,
        scope="openid email",
        data={"next": safe_next(next)},
        prompt="select_account",
    )


async def _match_user(db: AsyncSession, claims: dict[str, Any]) -> User:
    """The active user the verified Google identity may sign in as, else Rejected."""
    sub, email = str(claims["sub"]), str(claims["email"]).strip().lower()
    user = await db.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active or user.google_sub not in (None, sub):
        raise Rejected("no_account")
    other = await db.scalar(select(User.id).where(User.google_sub == sub, User.id != user.id))
    if other is not None:  # this Google account is already bound to another user
        raise Rejected("no_account")
    user.google_sub = sub
    return user


@router.get("/auth/google/callback")
async def google_callback(
    request: Request,
    db: Db,
    redis: Rds,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:  # role: public; state cookie + PKCE bind the flow to this browser
    try:
        data, tokens = await finish(
            request,
            redis,
            purpose="login",
            callback=LOGIN_CALLBACK,
            code=code,
            state=state,
            error=error,
        )
        async with google.http_client() as http:
            claims = await google.verify_id_token(
                http, str(tokens.get("id_token", "")), data["nonce"]
            )
        user = await _match_user(db, claims)
    except (Rejected, google.GoogleError) as exc:
        reason = exc.args[0] if isinstance(exc, Rejected) else "failed"
        log.info("auth.google_rejected reason=%s detail=%s", reason, exc)
        await db.rollback()
        if reason not in ("state", "cancelled"):  # those never reached Google's answer
            await record(
                db,
                action=AuditAction.LOGIN_FAIL,
                actor_id=None,
                target_type="user",
                target_id=None,
                ip=_ip(request),
                meta={"method": "google", "reason": reason},
            )
            await db.commit()
        return back(f"/login?error=google_{reason}")
    resp = back(safe_next(data.get("next")))
    start_session(db, request, resp, user)
    await record(
        db,
        action=AuditAction.LOGIN_SUCCESS,
        actor_id=user.id,
        target_type="user",
        target_id=user.id,
        ip=_ip(request),
        meta={"method": "google"},
    )
    await db.commit()
    return resp
