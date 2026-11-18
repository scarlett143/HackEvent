"""Calendar items (HE-060, doc 06 §A4; App Flow F6).

- `GET /calendar-items?from=&to=`: the caller's items overlapping [from, to), soonest first;
- `POST /calendar-items`: a personal item (`event_id` null);
- `PATCH /calendar-items/{id}`: drag / resize / edit;
- `DELETE /calendar-items/{id}`.

Registered events reach the calendar as items too: `POST /events/{id}/registration` adds
the user's own copy of the event's run in the same transaction, and unregistering removes
it (api/events.py `_set_interest`). Editing a copy never changes the event (doc 05).
Every query is scoped to the caller's user id; other users' items are 404 (doc 07 §3).
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from pydantic import AfterValidator, AwareDatetime, BaseModel, Field, model_validator
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.errors import ApiError
from api.roles import get_owned, owned, require_role
from common.models import CalendarItem, User, UserRole

router = APIRouter(prefix="/api/v1")

AnyUser = Annotated[User, Depends(require_role(*UserRole))]
Db = Annotated[AsyncSession, Depends(get_db)]

LIST_LIMIT = 1000  # ponytail: no paging; a month/agenda window is far below this
Title = Annotated[str, Field(min_length=1, max_length=200)]
Utc = Annotated[AwareDatetime, AfterValidator(lambda d: d.astimezone(UTC))]  # doc 06: UTC
Color = Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")]


class ItemOut(BaseModel):
    id: uuid.UUID
    event_id: uuid.UUID | None
    title: str
    starts_at: datetime
    ends_at: datetime
    all_day: bool
    color: str | None


class ItemList(BaseModel):
    items: list[ItemOut]


def _check_order(starts_at: datetime | None, ends_at: datetime | None) -> None:
    if starts_at and ends_at and ends_at < starts_at:
        raise ValueError("ends_at must not be before starts_at")


def _check_end(starts_at: datetime, ends_at: datetime) -> None:
    """A PATCH may move only one end; check the merged result."""
    if ends_at < starts_at:
        raise ApiError(422, "invalid_request", "ends_at must not be before starts_at.")


class ItemIn(BaseModel):
    title: Title
    starts_at: Utc
    ends_at: Utc
    all_day: bool = False
    color: Color | None = None

    @model_validator(mode="after")
    def _order(self) -> "ItemIn":
        _check_order(self.starts_at, self.ends_at)
        return self


class ItemPatch(BaseModel):
    title: Title | None = None
    starts_at: Utc | None = None
    ends_at: Utc | None = None
    all_day: bool | None = None
    color: Color | None = None

    @model_validator(mode="after")
    def _order(self) -> "ItemPatch":
        for f in ("title", "starts_at", "ends_at", "all_day"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        _check_order(self.starts_at, self.ends_at)
        return self


def _out(item: CalendarItem) -> ItemOut:
    return ItemOut.model_validate(item, from_attributes=True)


@router.get("/calendar-items")
async def list_items(  # role: any, own rows
    user: AnyUser,
    db: Db,
    from_: Annotated[AwareDatetime, Query(alias="from")],
    to: AwareDatetime,
) -> ItemList:
    rows = await db.scalars(
        owned(select(CalendarItem), CalendarItem, user)
        .where(CalendarItem.starts_at < to, CalendarItem.ends_at >= from_)
        .order_by(CalendarItem.starts_at, CalendarItem.id)
        .limit(LIST_LIMIT)
    )
    return ItemList(items=[_out(i) for i in rows])


@router.post("/calendar-items", status_code=201)
async def create_item(body: ItemIn, user: AnyUser, db: Db) -> ItemOut:  # role: any
    item = CalendarItem(user_id=user.id, event_id=None, **body.model_dump())
    db.add(item)
    await db.commit()
    return _out(item)


@router.patch("/calendar-items/{item_id}")
async def update_item(  # role: any, own row
    item_id: uuid.UUID, body: ItemPatch, user: AnyUser, db: Db
) -> ItemOut:
    item = await get_owned(db, CalendarItem, item_id, user)
    changes = body.model_dump(exclude_unset=True)
    _check_end(changes.get("starts_at", item.starts_at), changes.get("ends_at", item.ends_at))
    for k, v in changes.items():
        setattr(item, k, v)
    await db.commit()
    return _out(item)


@router.delete("/calendar-items/{item_id}", status_code=204)
async def delete_item(item_id: uuid.UUID, user: AnyUser, db: Db) -> Response:  # own row
    gone = await db.scalar(
        delete(CalendarItem)
        .where(CalendarItem.id == item_id, CalendarItem.user_id == user.id)
        .returning(CalendarItem.id)
    )
    if gone is None:
        raise ApiError(404, "not_found", "Not found.")
    await db.commit()
    return Response(status_code=204)
