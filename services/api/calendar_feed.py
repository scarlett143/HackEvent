"""Private calendar feed (HE-062): subscribe from Google / Apple / Outlook Calendar by URL.

- `GET /me/calendar-feed` -> `{enabled}`.
- `POST /me/calendar-feed` -> 201 `{url}`. Creates the feed, or ROTATES it (the old URL stops working
  at once). The token is shown only here: the database keeps just its SHA-256.
- `DELETE /me/calendar-feed` -> 204, revokes it.
- `GET /calendar/{token}.ics` is public (calendar apps cannot log in); the 256-bit token IS the
  credential and selects exactly one user, so it only ever returns that user's tracked and
  registered events. An unknown or rotated token is a plain 404. Logs redact the token
  (common.logging). Subscribing is one-way (HackEvents -> calendar); the calendar app polls.

ponytail: no per-token rate limit; the token is unguessable, add one at Caddy if it is ever scraped.
"""

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response
from pydantic import BaseModel
from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import current_user, hash_token
from api.deps import get_db
from api.errors import ApiError
from common import ics
from common.config import get_settings
from common.models import CalendarFeed, Event, EventStatus, Registration, Track, User

router = APIRouter(prefix="/api/v1")

Me = Annotated[User, Depends(current_user)]
Db = Annotated[AsyncSession, Depends(get_db)]

FEED_MAX_EVENTS = 500


class FeedStatus(BaseModel):
    enabled: bool


class FeedCreated(BaseModel):
    url: str


@router.get("/me/calendar-feed")
async def feed_status(user: Me, db: Db) -> FeedStatus:  # role: any; the caller's own row
    row = await db.scalar(select(CalendarFeed.id).where(CalendarFeed.user_id == user.id))
    return FeedStatus(enabled=row is not None)


@router.post("/me/calendar-feed", status_code=201)
async def create_or_rotate_feed(user: Me, db: Db) -> FeedCreated:  # role: any; own row only
    token = secrets.token_urlsafe(32)
    stmt = insert(CalendarFeed).values(user_id=user.id, token_hash=hash_token(token))
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[CalendarFeed.user_id], set_={"token_hash": stmt.excluded.token_hash}
        )
    )
    await db.commit()
    return FeedCreated(url=f"https://{get_settings().app_domain}/api/v1/calendar/{token}.ics")


@router.delete("/me/calendar-feed", status_code=204)
async def revoke_feed(user: Me, db: Db) -> Response:  # role: any; own row only
    await db.execute(delete(CalendarFeed).where(CalendarFeed.user_id == user.id))
    await db.commit()
    return Response(status_code=204)


@router.get("/calendar/{token}.ics")
async def feed(token: Annotated[str, Path(min_length=20, max_length=128)], db: Db) -> Response:
    """Public by design; the token resolves to one user and nothing else is reachable."""
    user_id = await db.scalar(
        select(CalendarFeed.user_id).where(CalendarFeed.token_hash == hash_token(token))
    )
    if user_id is None:
        raise ApiError(404, "not_found", "This calendar link is not valid.")
    mine = or_(
        Event.id.in_(select(Track.event_id).where(Track.user_id == user_id)),
        Event.id.in_(select(Registration.event_id).where(Registration.user_id == user_id)),
    )
    rows = await db.scalars(
        select(Event)
        .where(mine, Event.status != EventStatus.cancelled)
        .order_by(Event.registration_deadline.asc().nulls_last(), Event.id)
        .limit(FEED_MAX_EVENTS)
    )
    body = ics.build(
        (
            ics.FeedEvent(
                id=str(e.id),
                title=e.title,
                registration_deadline=e.registration_deadline,
                event_start=e.event_start,
                event_end=e.event_end,
                location=e.location,
            )
            for e in rows
        ),
        name="HackEvents",
    )
    return Response(
        body,
        media_type="text/calendar; charset=utf-8",
        headers={"Cache-Control": "private, max-age=300", "Content-Disposition": 'inline; filename="hackevents.ics"'},
    )
