"""Password login, sessions, logout and the reusable `current_user` dependency.

HE-011 / doc 07 §4 / doc 06 A1, A2, A8:
- Passwords: Argon2id (argon2-cffi defaults, RFC 9106 low-memory profile), min 12 chars,
  rejected if on the bundled common/breached list (`check_password_policy`, for HE-010).
- Sessions: random 32-byte token in an `HttpOnly; Secure; SameSite=Lax` cookie; only its
  sha256 is stored. 14-day sliding expiry, refreshed at most once per `SESSION_REFRESH_AFTER`.
- Login: 5 attempts / 15 min per IP and per email (Redis, fixed window). A successful login
  refunds its attempt, so in practice the limit counts failures (doc 07 "5 fails / 15 min").
- Bad credentials always get the same 401; unknown emails still pay for one Argon2 verify.

Tokens, cookies and passwords are never logged. CSRF (HE-015), roles (HE-013) and admin
TOTP (HE-014) build on `current_auth` / `current_user`.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Cookie, Depends, Request, Response
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db, get_redis
from api.errors import ApiError
from common.models import Session, User, UserRole

SESSION_COOKIE = "__Host-session"  # __Host-: Secure, Path=/, no Domain (blocks subdomain injection)
SESSION_TTL = timedelta(days=14)
SESSION_REFRESH_AFTER = timedelta(hours=1)
LOGIN_LIMIT = 5
LOGIN_WINDOW_SECONDS = 15 * 60
MIN_PASSWORD_LEN = 12
MAX_PASSWORD_LEN = 128

_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))
# ponytail: small bundled list; swap for a larger offline breached-password corpus if needed.
_COMMON_PASSWORDS = frozenset(
    line.strip().lower()
    for line in Path(__file__).with_name("common_passwords.txt").read_text().splitlines()
    if line.strip() and not line.startswith("#")
)


# --- passwords and tokens ---


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def check_password_policy(password: str) -> None:
    """Raise a 422 ApiError if `password` may not be set as a new password."""
    if len(password) < MIN_PASSWORD_LEN:
        raise ApiError(422, "weak_password", f"Use at least {MIN_PASSWORD_LEN} characters.")
    if len(password) > MAX_PASSWORD_LEN:
        raise ApiError(422, "weak_password", f"Use at most {MAX_PASSWORD_LEN} characters.")
    if password.lower() in _COMMON_PASSWORDS:
        raise ApiError(422, "weak_password", "This password is too common. Choose another.")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _verify(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


# --- rate limit ---


def _login_keys(ip: str, email: str) -> list[str]:
    return [f"rl:login:ip:{ip}", f"rl:login:email:{hash_token(email)}"]


async def _consume_attempt(redis: Redis, keys: list[str]) -> None:
    async with redis.pipeline(transaction=True) as pipe:
        for key in keys:
            pipe.incr(key)
            pipe.expire(key, LOGIN_WINDOW_SECONDS, nx=True)
        counts = (await pipe.execute())[0::2]
    if max(counts) > LOGIN_LIMIT:
        retry = max([int(await redis.ttl(k)) for k in keys] + [1])
        raise ApiError(
            429,
            "rate_limited",
            "Too many login attempts. Try again later.",
            headers={"Retry-After": str(retry)},
        )


async def _refund_attempt(redis: Redis, keys: list[str]) -> None:
    async with redis.pipeline(transaction=True) as pipe:
        for key in keys:
            pipe.decr(key)
        await pipe.execute()


# --- sessions ---


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


@dataclass
class Auth:
    user: User
    session: Session


async def current_auth(
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> Auth:
    """Resolve the session cookie to an active user, sliding the expiry forward."""
    if not token:
        raise ApiError(401, "unauthenticated", "Sign in to continue.")
    now = datetime.now(UTC)
    row = (
        await db.execute(
            select(Session, User)
            .join(User, User.id == Session.user_id)
            .where(Session.token_hash == hash_token(token))
            .where(Session.expires_at > now)
            .where(User.is_active.is_(True))
        )
    ).first()
    if row is None:
        raise ApiError(401, "unauthenticated", "Sign in to continue.")
    session, user = row
    if session.expires_at - now < SESSION_TTL - SESSION_REFRESH_AFTER:
        session.expires_at = now + SESSION_TTL
        await db.commit()
        _set_session_cookie(response, token)
    return Auth(user=user, session=session)


async def current_user(auth: Annotated[Auth, Depends(current_auth)]) -> User:
    return auth.user


# --- endpoints ---

router = APIRouter(prefix="/api/v1")


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class MeOut(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    role: UserRole
    timezone: str


def _me(user: User) -> MeOut:
    return MeOut(
        id=user.id, email=user.email, name=user.name, role=user.role, timezone=user.timezone
    )


@router.post("/auth/login")
async def login(
    body: LoginIn,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> MeOut:  # role: public
    email = body.email.strip().lower()
    ip = request.client.host if request.client else "unknown"
    keys = _login_keys(ip, email)
    await _consume_attempt(redis, keys)

    user = await db.scalar(select(User).where(User.email == email))
    stored = user.password_hash if user is not None else None
    password_ok = _verify(stored or _DUMMY_HASH, body.password) and stored is not None
    if user is None or stored is None or not password_ok or not user.is_active:
        raise ApiError(401, "invalid_credentials", "Invalid email or password.")

    await _refund_attempt(redis, keys)
    if _hasher.check_needs_rehash(stored):
        user.password_hash = hash_password(body.password)
    token = secrets.token_urlsafe(32)
    db.add(
        Session(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=datetime.now(UTC) + SESSION_TTL,
            ip=ip,
            user_agent=request.headers.get("user-agent", "")[:512],
        )
    )
    await db.commit()
    _set_session_cookie(response, token)
    return _me(user)


@router.post("/auth/logout", status_code=204)
async def logout(
    response: Response,
    auth: Annotated[Auth, Depends(current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:  # role: any signed-in user; deletes only the caller's own session
    await db.execute(delete(Session).where(Session.id == auth.session.id))
    await db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")


@router.get("/me")
async def me(user: Annotated[User, Depends(current_user)]) -> MeOut:  # role: any
    return _me(user)
