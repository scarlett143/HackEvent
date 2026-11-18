"""College-wide dashboard numbers (HE-110, App Flow F9, doc 06 A6).

`summary(db)` is the body of `GET /dashboard/summary` (Management + Admin, doc 07 §3) and
of the weekly digest. Rejected events are invisible, as in the feed; "open" is the feed's
effective status (status open and the deadline not yet passed). Registrations are
counted on visible events, all time.
"""

import uuid
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from common.models import (
    Event,
    EventStatus,
    Organisation,
    Registration,
    ReviewStatus,
    User,
    UserRole,
)

CLOSING_WINDOW = timedelta(days=7)  # App Flow F9 "closing this week"
CLOSING_LIMIT = 20


class ClosingEvent(BaseModel):
    id: uuid.UUID
    title: str
    organiser: str
    registration_deadline: datetime
    registered_by: list[str]  # user names


class UserRegistrations(BaseModel):
    user_id: uuid.UUID
    name: str
    role: UserRole
    registrations: int


class KindRegistrations(BaseModel):
    kind: str  # company | institution | unmatched (no organisation matched)
    registrations: int


class Summary(BaseModel):
    generated_at: datetime
    open_events: int
    closing_this_week: int
    new_this_week: int
    closing_soon: list[ClosingEvent]
    registrations_by_user: list[UserRegistrations]
    registrations_by_kind: list[KindRegistrations]


def _visible() -> ColumnElement[bool]:
    return Event.review_status != ReviewStatus.rejected


def _open(now: datetime) -> ColumnElement[bool]:
    deadline = Event.registration_deadline
    return and_(
        _visible(),
        Event.status == EventStatus.open,
        or_(deadline.is_(None), deadline >= now),
    )


async def summary(db: AsyncSession, *, now: datetime | None = None) -> Summary:
    now = now or datetime.now(UTC)
    closing = and_(_open(now), Event.registration_deadline < now + CLOSING_WINDOW)
    count = select(func.count()).select_from(Event)
    open_events = await db.scalar(count.where(_open(now))) or 0
    closing_this_week = await db.scalar(count.where(closing)) or 0
    new_this_week = (
        await db.scalar(count.where(_visible(), Event.first_seen_at >= now - CLOSING_WINDOW)) or 0
    )

    # One registration per (user, event): no DISTINCT, so two people with the same name both count.
    names = func.array_remove(func.array_agg(User.name), None)
    soon = await db.execute(
        select(Event.id, Event.title, Event.organiser_name, Event.registration_deadline, names)
        .outerjoin(Registration, Registration.event_id == Event.id)
        .outerjoin(User, User.id == Registration.user_id)
        .where(closing)
        .group_by(Event.id)
        .order_by(Event.registration_deadline, Event.id)
        .limit(CLOSING_LIMIT)
    )

    regs = (
        select(Registration.user_id, Registration.event_id, Event.organisation_id)
        .join(Event, Event.id == Registration.event_id)
        .where(_visible())
        .subquery()
    )
    n = func.count(regs.c.event_id)
    by_user = await db.execute(
        select(User.id, User.name, User.role, n)
        .outerjoin(regs, regs.c.user_id == User.id)
        .where(User.is_active)
        .group_by(User.id)
        .order_by(n.desc(), User.name)
    )
    by_kind = await db.execute(
        select(Organisation.kind, func.count())
        .select_from(regs)
        .outerjoin(Organisation, Organisation.id == regs.c.organisation_id)
        .group_by(Organisation.kind)
        .order_by(func.count().desc())
    )
    return Summary(
        generated_at=now,
        open_events=open_events,
        closing_this_week=closing_this_week,
        new_this_week=new_this_week,
        closing_soon=[
            ClosingEvent(
                id=r[0],
                title=" ".join(r[1].split()),
                organiser=" ".join(r[2].split()),
                registration_deadline=r[3],
                registered_by=sorted(r[4]),
            )
            for r in soon
        ],
        registrations_by_user=[
            UserRegistrations(user_id=r[0], name=r[1], role=r[2], registrations=r[3])
            for r in by_user
        ],
        registrations_by_kind=[
            KindRegistrations(kind=r[0].value if r[0] else "unmatched", registrations=r[1])
            for r in by_kind
        ],
    )
