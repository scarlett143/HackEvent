"""In-app notification centre (HE-073, doc 06 §A2): the in-app channel's inbox.

- `GET /notifications`: the caller's newest notifications (+ unread count);
- `POST /notifications/read`: `{"ids": [...]}` or `{"all": true}` marks them read.

Every query is filtered by the caller's user id (row ownership, doc 07 §3): another
user's ids are silently ignored, never revealed. Title / body are plain text built by
our notifier code from already plain-text event fields; the UI renders them as text.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.roles import require_role
from common.models import Notification, User, UserRole

router = APIRouter(prefix="/api/v1")

AnyUser = Annotated[User, Depends(require_role(*UserRole))]
Db = Annotated[AsyncSession, Depends(get_db)]

LIST_LIMIT = 50
READ_MAX_IDS = 200


class NotificationOut(BaseModel):
    id: uuid.UUID
    type: str
    title: str
    body: str
    event_id: uuid.UUID | None
    created_at: datetime
    read_at: datetime | None


class NotificationList(BaseModel):
    items: list[NotificationOut]
    unread: int


class ReadIn(BaseModel):
    ids: list[uuid.UUID] = Field(default_factory=list, max_length=READ_MAX_IDS)
    all: bool = False

    @model_validator(mode="after")
    def _one_of(self) -> "ReadIn":
        if self.all == bool(self.ids):
            raise ValueError('send either "ids" or "all": true')
        return self


class ReadOut(BaseModel):
    updated: int


@router.get("/notifications")
async def list_notifications(user: AnyUser, db: Db) -> NotificationList:  # role: any, own rows
    mine = Notification.user_id == user.id
    rows = await db.scalars(
        select(Notification)
        .where(mine)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(LIST_LIMIT)
    )
    unread = await db.scalar(
        select(func.count()).select_from(Notification).where(mine, Notification.read_at.is_(None))
    )
    return NotificationList(
        items=[NotificationOut.model_validate(n, from_attributes=True) for n in rows],
        unread=unread or 0,
    )


@router.post("/notifications/read")
async def mark_read(body: ReadIn, user: AnyUser, db: Db) -> ReadOut:  # role: any, own rows
    stmt = (
        update(Notification)
        .where(Notification.user_id == user.id, Notification.read_at.is_(None))
        .values(read_at=func.now())
        .returning(Notification.id)
    )
    if not body.all:
        stmt = stmt.where(Notification.id.in_(body.ids))
    updated = len((await db.execute(stmt.execution_options(synchronize_session=False))).all())
    await db.commit()
    return ReadOut(updated=updated)
