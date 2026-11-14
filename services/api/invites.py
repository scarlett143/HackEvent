"""Invites (HE-010, Security doc §4, doc 06 A1 + A7, App Flow F1).

- `POST /admin/invites` (Admin with a TOTP-verified session, HE-014): token = `secrets.token_urlsafe(32)`; only its sha256 is
  stored; expires 72 h after creation. The email goes out via the NotifyWorker
  `send_email` job, enqueued before commit, so a failed enqueue leaves no invite behind.
- `GET /invites/{token}` (public): validates without consuming.
- `POST /invites/{token}/accept` (public): name + password -> user + session. The invite is
  claimed with one conditional UPDATE (unused, unexpired), so concurrent accepts can't
  both win. Unknown, used and expired tokens all get the same 404.

Tokens are never logged (the log filter redacts `/invites/<token>` paths) nor audited.
"""

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, StringConstraints
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import (
    Auth,
    MeOut,
    _me,
    check_password_policy,
    hash_password,
    hash_token,
    start_session,
)
from api.deps import get_db, get_redis
from api.errors import ApiError
from api.totp import require_admin_2fa
from common.audit import AuditAction, record
from common.models import Invite, User, UserRole
from common.queues import NOTIFY_QUEUE

INVITE_TTL = timedelta(hours=72)
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

router = APIRouter(prefix="/api/v1")


def _now() -> datetime:
    return datetime.now(UTC)  # patched in tests to move the clock


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _invalid() -> ApiError:
    return ApiError(404, "invalid_invite", "This invite link is invalid or has expired.")


class InviteIn(BaseModel):
    email: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, to_lower=True, max_length=320, pattern=EMAIL_PATTERN
        ),
    ]
    role: UserRole


class InviteOut(BaseModel):
    id: uuid.UUID
    email: str
    role: UserRole
    expires_at: datetime


class InviteInfo(BaseModel):
    email: str
    role: UserRole


class AcceptIn(BaseModel):
    name: str = Field(min_length=1, max_length=200, pattern=r"\S")
    password: str = Field(min_length=1, max_length=1024)


@router.post("/admin/invites", status_code=201)
async def create_invite(
    body: InviteIn,
    request: Request,
    auth: Annotated[Auth, Depends(require_admin_2fa)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> InviteOut:
    admin, email = auth.user, body.email
    if await db.scalar(select(User.id).where(User.email == email)) is not None:
        raise ApiError(409, "already_registered", "A user with this email already exists.")
    token = secrets.token_urlsafe(32)
    invite = Invite(
        email=email,
        role=body.role,
        token_hash=hash_token(token),
        expires_at=_now() + INVITE_TTL,
        created_by=admin.id,
    )
    db.add(invite)
    await db.flush()
    await record(
        db,
        action=AuditAction.INVITE_CREATE,
        actor_id=admin.id,
        target_type="invite",
        target_id=invite.id,
        ip=_ip(request),
        meta={"email": email, "role": body.role.value},
    )
    # The pool is the app's; ArqRedis only adds enqueue_job on top of it.
    await ArqRedis(redis.connection_pool).enqueue_job(
        "send_email",
        email,
        "invite",
        {"token": token, "inviter": admin.name},
        _queue_name=NOTIFY_QUEUE,
    )
    await db.commit()
    return InviteOut(id=invite.id, email=email, role=invite.role, expires_at=invite.expires_at)


@router.get("/invites/{token}")
async def check_invite(
    token: str, db: Annotated[AsyncSession, Depends(get_db)]
) -> InviteInfo:  # role: public; reveals the invited email only to the token holder
    invite = await db.scalar(
        select(Invite)
        .where(Invite.token_hash == hash_token(token))
        .where(Invite.used_at.is_(None))
        .where(Invite.expires_at > _now())
    )
    if invite is None:
        raise _invalid()
    return InviteInfo(email=invite.email, role=invite.role)


@router.post("/invites/{token}/accept", status_code=201)
async def accept_invite(
    token: str,
    body: AcceptIn,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MeOut:  # role: public; the token is the credential
    check_password_policy(body.password)  # before claiming, so a weak password keeps the link
    now = _now()
    claimed = (
        await db.execute(
            update(Invite)
            .where(Invite.token_hash == hash_token(token))
            .where(Invite.used_at.is_(None))
            .where(Invite.expires_at > now)
            .values(used_at=now)
            .returning(Invite.id, Invite.email, Invite.role)
        )
    ).first()
    if claimed is None:
        raise _invalid()
    invite_id, email, role = claimed
    user = User(
        email=email, name=body.name.strip(), role=role, password_hash=hash_password(body.password)
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:  # email registered since the invite was sent
        await db.rollback()
        raise _invalid() from None
    await record(
        db,
        action=AuditAction.INVITE_ACCEPT,
        actor_id=user.id,
        target_type="invite",
        target_id=invite_id,
        ip=_ip(request),
        meta={"role": role.value},
    )
    start_session(db, request, response, user)
    await db.commit()
    await db.refresh(user)  # server defaults (timezone)
    return _me(user)
