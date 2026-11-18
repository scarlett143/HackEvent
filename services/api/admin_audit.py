"""Audit log viewer (HE-096, doc 06 A7 `GET /admin/audit`, doc 07 §11).

`GET /admin/audit?actor=&action=&cursor=&limit=`: newest first, keyset cursor on
(at, id). `actor` is a user id; `action` is one of `AuditAction`. Each row carries the
actor's email (null once the actor is deleted: doc 07 §10 nulls `actor_id`). `meta` is
already sanitised by `common.audit.record`; clients render it as text.

Admin + 2FA on every route (router dependency, doc 07 §3).
"""

import base64
import binascii
import json
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.errors import ApiError
from api.totp import require_admin_2fa
from common.audit import AuditAction
from common.models import AuditLog, User

router = APIRouter(prefix="/api/v1/admin", dependencies=[Depends(require_admin_2fa)])

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class AuditOut(BaseModel):
    id: uuid.UUID
    at: datetime
    action: str
    actor_id: uuid.UUID | None
    actor_email: str | None
    target_type: str
    target_id: uuid.UUID | None
    ip: str
    meta: dict[str, Any]


class AuditPage(BaseModel):
    items: list[AuditOut]
    next_cursor: str | None


def _encode(row: AuditLog) -> str:
    raw = json.dumps([row.at.isoformat(), str(row.id)])
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        at_raw, id_raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        at = datetime.fromisoformat(at_raw)
        if at.tzinfo is None:
            raise ValueError("naive datetime")
        return at, uuid.UUID(id_raw)
    except (binascii.Error, ValueError, TypeError, AttributeError) as exc:
        raise ApiError(400, "invalid_cursor", "Invalid cursor.") from exc


@router.get("/audit")
async def list_audit(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: uuid.UUID | None = None,
    action: AuditAction | None = None,
    cursor: Annotated[str | None, Query(max_length=500)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> AuditPage:  # role: admin + 2FA; admins see every entry (doc 07 §3 matrix)
    stmt = select(AuditLog, User.email).outerjoin(User, User.id == AuditLog.actor_id)
    if actor is not None:
        stmt = stmt.where(AuditLog.actor_id == actor)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action.value)
    if cursor:
        stmt = stmt.where(tuple_(AuditLog.at, AuditLog.id) < tuple_(*_decode(cursor)))
    stmt = stmt.order_by(AuditLog.at.desc(), AuditLog.id.desc()).limit(limit + 1)
    rows = list((await db.execute(stmt)).all())
    page, more = rows[:limit], len(rows) > limit
    return AuditPage(
        items=[
            AuditOut(
                id=r.id,
                at=r.at,
                action=r.action,
                actor_id=r.actor_id,
                actor_email=email,
                target_type=r.target_type,
                target_id=r.target_id,
                ip=r.ip,
                meta=r.meta,
            )
            for r, email in page
        ],
        next_cursor=_encode(page[-1][0]) if more else None,
    )
