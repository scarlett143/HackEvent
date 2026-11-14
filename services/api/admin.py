"""Admin API (doc 06 A7). The router-level `require_admin_2fa` gates every route (HE-014).

Users + invites (HE-090): `GET /admin/users`, `PATCH /admin/users/{id}` (role, is_active),
`GET /admin/invites`. Invite creation lives in api.invites. Responses never carry password
hashes, TOTP secrets, session or invite tokens/hashes.
"""

import uuid
from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import Auth
from api.deps import get_db
from api.errors import ApiError
from api.invites import _ip, _now
from api.totp import require_admin_2fa
from common.audit import AuditAction, record
from common.models import Invite, Session, User, UserRole

router = APIRouter(prefix="/api/v1/admin", dependencies=[Depends(require_admin_2fa)])

RECENT_INVITES = timedelta(days=30)
MAX_ROWS = 500


@router.get("/ping")
async def ping() -> dict[str, str]:  # role: admin + 2FA (router dependency)
    """Proves the /admin gate; the web 2FA page uses it to learn the session's state."""
    return {"status": "ok"}


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    role: UserRole
    is_active: bool
    created_at: datetime


class UserList(BaseModel):
    items: list[UserOut]
    next_cursor: None = None  # doc 06 list shape; see MAX_ROWS


class UserPatch(BaseModel):
    role: UserRole | None = None
    is_active: bool | None = None


InviteStatus = Literal["pending", "used", "expired"]


class InviteRow(BaseModel):
    id: uuid.UUID
    email: str
    role: UserRole
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None
    status: InviteStatus


class InviteList(BaseModel):
    items: list[InviteRow]
    next_cursor: None = None


def _user_out(u: User) -> UserOut:
    return UserOut(
        id=u.id,
        email=u.email,
        name=u.name,
        role=u.role,
        is_active=u.is_active,
        created_at=u.created_at,
    )


@router.get("/users")
async def list_users(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserList:  # role: admin + 2FA; admins see every user (doc 07 §3)
    # ponytail: invite-only team, so one page of MAX_ROWS; add the doc 06 cursor when it grows.
    users = await db.scalars(select(User).order_by(User.created_at, User.id).limit(MAX_ROWS))
    return UserList(items=[_user_out(u) for u in users])


@router.patch("/users/{user_id}")
async def update_user(
    user_id: uuid.UUID,
    body: UserPatch,
    request: Request,
    auth: Annotated[Auth, Depends(require_admin_2fa)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserOut:  # role: admin + 2FA
    admin = auth.user
    target = await db.scalar(select(User).where(User.id == user_id).with_for_update())
    if target is None:
        raise ApiError(404, "not_found", "Not found.")
    if target.id == admin.id and (
        (body.role is not None and body.role != UserRole.admin) or body.is_active is False
    ):
        # The caller is an admin, so refusing self-demotion keeps at least one admin.
        # ponytail: two admins demoting each other concurrently can still both lose it;
        # lock the admin set if that ever matters.
        raise ApiError(409, "cannot_change_self", "You can't demote or deactivate yourself.")
    ip = _ip(request)
    if body.role is not None and body.role != target.role:
        await record(
            db,
            action=AuditAction.USER_ROLE_CHANGE,
            actor_id=admin.id,
            target_type="user",
            target_id=target.id,
            ip=ip,
            meta={"from": target.role.value, "to": body.role.value},
        )
        target.role = body.role
    if body.is_active is not None and body.is_active != target.is_active:
        # Security doc §11 has no separate reactivate action; meta says which way it went.
        await record(
            db,
            action=AuditAction.USER_DEACTIVATE,
            actor_id=admin.id,
            target_type="user",
            target_id=target.id,
            ip=ip,
            meta={"is_active": body.is_active},
        )
        target.is_active = body.is_active
        if not body.is_active:  # end every session now, not just at the next request
            await db.execute(delete(Session).where(Session.user_id == target.id))
    await db.commit()
    return _user_out(target)


@router.get("/invites")
async def list_invites(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InviteList:  # role: admin + 2FA; pending plus the last RECENT_INVITES of history
    now = _now()
    pending = Invite.used_at.is_(None) & (Invite.expires_at > now)
    invites = await db.scalars(
        select(Invite)
        .where(or_(pending, Invite.created_at > now - RECENT_INVITES))
        .order_by(Invite.created_at.desc(), Invite.id)
        .limit(MAX_ROWS)
    )
    return InviteList(
        items=[
            InviteRow(
                id=i.id,
                email=i.email,
                role=i.role,
                created_at=i.created_at,
                expires_at=i.expires_at,
                used_at=i.used_at,
                status="used" if i.used_at else "pending" if i.expires_at > now else "expired",
            )
            for i in invites
        ]
    )
