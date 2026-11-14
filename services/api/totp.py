"""Admin TOTP 2FA (HE-014 / doc 07 §4 "Admin: TOTP 2FA required", §5 TOTP secrets encrypted).

- RFC 6238 with the stdlib: HMAC-SHA1, 30 s step, 6 digits, ±1 step tolerance.
- A code is single use: `users.totp_last_step` only moves forward, via a conditional UPDATE,
  so a replayed (or concurrently double-submitted) code is rejected.
- The secret is stored only in `users.totp_secret_enc` (AES-256-GCM envelope, row-bound AAD).
  While `users.totp_enabled_at` is null the stored secret is a pending enrolment.
- A session is 2FA-verified when `sessions.totp_verified_at` is set: after login, by
  `POST /auth/2fa/verify` (or a `totp` field on login), or by confirming enrolment.
- Code attempts: 5 / 15 min per user (same Redis limiter as login).

Flow for the UI: admin route → 403 `totp_enrolment_required` → enrol + confirm;
403 `totp_required` → verify. No recovery codes: the docs do not call for them; a lost
device is reset by an operator clearing the totp_* columns.
"""

import base64
import hashlib
import hmac
import os
import struct
import time
from datetime import UTC, datetime
from typing import Annotated, cast
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import CursorResult, or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import Auth, _consume_attempt, _refund_attempt, current_auth
from api.deps import get_db, get_redis
from api.errors import ApiError
from api.roles import require_role
from common import crypto
from common.config import get_settings
from common.models import User, UserRole

STEP_SECONDS = 30
DIGITS = 6
WINDOW = 1  # steps either side of now
ISSUER = "HackEvents"

# --- RFC 6238 ---


def new_secret() -> str:
    """160-bit random secret, base32 without padding (what authenticator apps expect)."""
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    return base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // STEP_SECONDS)


def match_step(secret: str, code: str, now: float | None = None) -> int | None:
    """Return the time-step `code` is valid for (within ±WINDOW), else None."""
    if len(code) != DIGITS or not code.isdigit():
        return None
    base = current_step(now)
    found = None
    for step in range(base - WINDOW, base + WINDOW + 1):  # no early exit: constant work
        if hmac.compare_digest(code_at(secret, step), code):
            found = step
    return found


def provisioning_uri(secret: str, account: str) -> str:
    label = quote(f"{ISSUER}:{account}")
    query = urlencode(
        {
            "secret": secret,
            "issuer": ISSUER,
            "algorithm": "SHA1",
            "digits": DIGITS,
            "period": STEP_SECONDS,
        }
    )
    return f"otpauth://totp/{label}?{query}"


# --- secret storage ---


def _require_master_key() -> None:
    if get_settings().master_key is None:
        raise ApiError(503, "totp_unavailable", "MASTER_KEY is not configured; TOTP is off.")


def _aad(user: User) -> bytes:
    return crypto.aad("users", "totp_secret_enc", user.id)


def _encrypt_secret(user: User, secret: str) -> bytes:
    _require_master_key()
    return crypto.encrypt(secret.encode(), _aad(user))


def _decrypt_secret(user: User) -> str:
    _require_master_key()
    assert user.totp_secret_enc is not None
    return crypto.decrypt(user.totp_secret_enc, _aad(user)).decode()


# --- code check ---


async def check_code(db: AsyncSession, redis: Redis, user: User, code: str) -> None:
    """Accept `code` for `user` once, or raise 400 invalid_totp / 429 rate_limited.

    Claims the time-step inside the caller's transaction; the caller commits.
    """
    keys = [f"rl:totp:user:{user.id}"]
    await _consume_attempt(redis, keys)
    step = match_step(_decrypt_secret(user), code)
    if step is not None:
        result = cast(
            CursorResult[tuple[()]],
            await db.execute(
                update(User)
                .where(User.id == user.id)
                .where(or_(User.totp_last_step.is_(None), User.totp_last_step < step))
                .values(totp_last_step=step)
                .execution_options(synchronize_session=False)
            ),
        )
        if result.rowcount == 1:
            user.totp_last_step = step
            await _refund_attempt(redis, keys)
            return
    raise ApiError(400, "invalid_totp", "Invalid or already used code.")


# --- dependencies ---


async def admin_auth(
    auth: Annotated[Auth, Depends(current_auth)],
    _admin: Annotated[User, Depends(require_role(UserRole.admin))],
) -> Auth:
    """Admin role (HE-013 `require_role`); returns the Auth so the session is reachable."""
    return auth


async def require_admin_2fa(auth: Annotated[Auth, Depends(admin_auth)]) -> Auth:
    """Admin role AND enrolled AND this session passed TOTP. Use on every /admin route."""
    if auth.user.totp_enabled_at is None:
        raise ApiError(403, "totp_enrolment_required", "Set up two-factor authentication.")
    if auth.session.totp_verified_at is None:
        raise ApiError(403, "totp_required", "Enter your two-factor code.")
    return auth


# --- endpoints ---

router = APIRouter(prefix="/api/v1/auth/2fa")


class CodeIn(BaseModel):
    code: str = Field(min_length=1, max_length=16)


class EnrolOut(BaseModel):
    secret: str
    otpauth_uri: str


class VerifiedOut(BaseModel):
    verified: bool


@router.post("/enrol")
async def enrol(
    auth: Annotated[Auth, Depends(admin_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> EnrolOut:  # role: admin (password session); only while 2FA is not yet enabled
    user = auth.user
    if user.totp_enabled_at is not None:
        raise ApiError(409, "totp_already_enabled", "Two-factor authentication is already on.")
    secret = new_secret()
    user.totp_secret_enc = _encrypt_secret(user, secret)
    user.totp_last_step = None
    await db.commit()
    return EnrolOut(secret=secret, otpauth_uri=provisioning_uri(secret, user.email))


@router.post("/confirm")
async def confirm(
    body: CodeIn,
    auth: Annotated[Auth, Depends(admin_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> VerifiedOut:  # role: admin; activates the pending secret and verifies this session
    user = auth.user
    if user.totp_enabled_at is not None:
        raise ApiError(409, "totp_already_enabled", "Two-factor authentication is already on.")
    if user.totp_secret_enc is None:
        raise ApiError(409, "totp_not_enrolled", "Start two-factor setup first.")
    await check_code(db, redis, user, body.code.strip())
    now = datetime.now(UTC)
    user.totp_enabled_at = now
    auth.session.totp_verified_at = now
    await db.commit()
    return VerifiedOut(verified=True)


@router.post("/verify")
async def verify(
    body: CodeIn,
    auth: Annotated[Auth, Depends(admin_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> VerifiedOut:  # role: admin; marks only the caller's own session verified
    if auth.user.totp_enabled_at is None:
        raise ApiError(409, "totp_not_enrolled", "Set up two-factor authentication first.")
    await check_code(db, redis, auth.user, body.code.strip())
    auth.session.totp_verified_at = datetime.now(UTC)
    await db.commit()
    return VerifiedOut(verified=True)
