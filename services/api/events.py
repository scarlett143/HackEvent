"""Event read API + per-user interest (HE-050, HE-054/055 API part; doc 06 A2 + A3).

- `GET /events`: feed. Filters (doc 06 A3; enum filters take comma lists, e.g.
  `status=open,closed`): `kind, tier, region` (of the matched organisation), `mode`,
  `status` (`open` | `closing` | `closed` | `cancelled`; default `open`, so closed and
  cancelled events are hidden unless asked), `deadline_before`, `deadline_after`,
  `registered` (true/false), `q` (title/organiser substring), `new_only`.
  Order: `registration_deadline` ascending, no-deadline events last, `id` breaks ties.
  Keyset cursor = opaque urlsafe base64 of JSON `[deadline ISO | null, id]`.
- `GET /events/{id}`: same object + description, prize. Every event carries `sources`
  (url, plain-text snippet, found_at, via host) and `needs_review`.
- `POST/DELETE /events/{id}/registration`, `POST/DELETE /events/{id}/track`: idempotent,
  always scoped to the current user's row (doc 07 §3).
- `GET /me/unread-count`, `POST /me/seen`: "New since last visit" (App Flow F2).

Effective status is computed at read time, because `events.status` is only refreshed when
a source re-reports the event: a past deadline reads as `closed`. `closing` = open with
the deadline inside CLOSING_WINDOW. Rejected events (admin review) are invisible here.
Scraped text is untrusted: snippets, descriptions and prizes go out as plain text only.
"""

import base64
import binascii
import html
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import (
    ColumnElement,
    String,
    and_,
    case,
    cast,
    delete,
    exists,
    func,
    literal,
    or_,
    select,
    true,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.errors import ApiError
from api.roles import require_role
from common import reminders
from common.models import (
    Event,
    EventMode,
    EventSource,
    EventStatus,
    Notification,
    Organisation,
    OrgKind,
    OrgRegion,
    Registration,
    ReviewStatus,
    Track,
    User,
    UserRole,
)

router = APIRouter(prefix="/api/v1")

# doc 07 §3: every role may view the feed and register/track for themselves.
AnyUser = Annotated[User, Depends(require_role(*UserRole))]
Db = Annotated[AsyncSession, Depends(get_db)]

CLOSING_WINDOW = timedelta(days=7)  # design brief 4.1 "Closing this week"
DEFAULT_LIMIT = 50
MAX_LIMIT = 100

FeedStatus = Literal["open", "closing", "closed", "cancelled"]


# --- plain text --------------------------------------------------------------------------

_TAG = re.compile(r"<[a-zA-Z/!?][^>]*>?")


def plain_text(value: str) -> str:
    """Scraped HTML -> plain text: entities decoded and tags dropped until stable, so an
    escaped tag (`&lt;b&gt;`) can't survive as a tag either."""
    prev = None
    while prev != value:
        prev, value = value, _TAG.sub(" ", html.unescape(value))
    return " ".join(value.split())


# --- response shapes (doc 06 A3 event object) --------------------------------------------


class OrganiserOut(BaseModel):
    name: str
    id: uuid.UUID | None
    kind: OrgKind | None
    tier: int | None
    region: OrgRegion | None


class SourceOut(BaseModel):
    url: str
    snippet: str
    found_at: datetime
    via: str  # host of the source page, e.g. "devpost.com"


class EventOut(BaseModel):
    id: uuid.UUID
    title: str
    organiser: OrganiserOut
    registration_url: str
    registration_deadline: datetime | None
    event_start: datetime | None
    event_end: datetime | None
    mode: EventMode
    location: str | None
    eligibility: list[str]
    status: Literal["open", "closed", "cancelled"]
    review_status: ReviewStatus
    needs_review: bool
    deadline_changed_at: datetime | None  # HE-072: "Deadline updated" chip for 48 h
    is_new: bool
    is_registered: bool
    is_tracked: bool
    sources: list[SourceOut]


class EventDetailOut(EventOut):
    description: str
    prize: str | None


class EventPage(BaseModel):
    items: list[EventOut]
    next_cursor: str | None


class RegistrationOut(BaseModel):
    is_registered: bool


class TrackOut(BaseModel):
    is_tracked: bool


class UnreadCount(BaseModel):
    new_events: int
    notifications: int


class SeenOut(BaseModel):
    last_seen_at: datetime


# --- query building ----------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _visible() -> ColumnElement[bool]:
    return Event.review_status != ReviewStatus.rejected


def _region_expr() -> Any:
    """Event region for the India/Global filter.

    The matched organisation's region wins (doc 06). Most scraped events have no matched
    organisation, so fall back to the event itself: a location naming India, or a source on
    Unstop (an India-only platform), means "india"; anything else is "global" (= outside India).
    ponytail: heuristic; replace with an extracted event-level region if one is added (doc 08).
    """
    from_unstop = exists().where(
        EventSource.event_id == Event.id,
        EventSource.source_url.op("~*")(r"^https?://([a-z0-9-]+\.)*unstop\.com(/|$)"),
    )
    return case(
        (Organisation.region.is_not(None), cast(Organisation.region, String)),
        (Event.location.ilike("%india%"), literal("india")),
        (from_unstop, literal("india")),
        else_=literal("global"),
    )


def _status_clause(status: FeedStatus, now: datetime) -> ColumnElement[bool]:
    deadline = Event.registration_deadline
    is_open = and_(Event.status == EventStatus.open, or_(deadline.is_(None), deadline >= now))
    if status == "open":
        return is_open
    if status == "closing":
        return and_(is_open, deadline < now + CLOSING_WINDOW)
    if status == "closed":
        return or_(
            Event.status == EventStatus.closed,
            and_(Event.status == EventStatus.open, deadline < now),
        )
    return Event.status == EventStatus.cancelled


def _is_new(user: User) -> ColumnElement[bool]:
    # Never visited -> everything is new.
    return true() if user.last_seen_at is None else Event.first_seen_at > user.last_seen_at


def _mine(model: type[Registration] | type[Track], user: User) -> ColumnElement[bool]:
    return exists().where(model.event_id == Event.id, model.user_id == user.id)


def _base(user: User) -> Any:
    return (
        select(
            Event,
            Organisation,
            _is_new(user).label("is_new"),
            _mine(Registration, user).label("is_registered"),
            _mine(Track, user).label("is_tracked"),
        )
        .outerjoin(Organisation, Organisation.id == Event.organisation_id)
        .where(_visible())
    )


def _encode_cursor(ev: Event) -> str:
    d = ev.registration_deadline
    raw = json.dumps([d.isoformat() if d else None, str(ev.id)])
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _after_cursor(cursor: str) -> ColumnElement[bool]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        d_raw, id_raw = json.loads(raw)
        last_id = uuid.UUID(id_raw)
        d = datetime.fromisoformat(d_raw) if d_raw is not None else None
        if d is not None and d.tzinfo is None:
            raise ValueError("naive datetime")
    except (binascii.Error, ValueError, TypeError, AttributeError) as exc:
        raise ApiError(400, "invalid_cursor", "Invalid cursor.") from exc
    deadline = Event.registration_deadline
    if d is None:  # already in the no-deadline tail
        return and_(deadline.is_(None), Event.id > last_id)
    return or_(deadline > d, and_(deadline == d, Event.id > last_id), deadline.is_(None))


def _csv[E](raw: str | None, parse: Any, name: str) -> list[E] | None:
    if raw is None:
        return None
    try:
        values: list[E] = [parse(v.strip()) for v in raw.split(",") if v.strip()]
    except ValueError as exc:
        raise ApiError(
            400, "invalid_filter", f"Invalid value for {name}.", {"filter": name}
        ) from exc
    if not values:
        raise ApiError(400, "invalid_filter", f"Invalid value for {name}.", {"filter": name})
    return values


def _parse_status(v: str) -> FeedStatus:
    if v not in ("open", "closing", "closed", "cancelled"):
        raise ValueError(v)
    return v  # type: ignore[return-value]


def _parse_tier(v: str) -> int:
    tier = int(v)
    if tier not in (1, 2, 3):
        raise ValueError(v)
    return tier


def _utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)  # doc 06: times are UTC
    return value


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# --- serialisation -----------------------------------------------------------------------


def _effective_status(ev: Event, now: datetime) -> Literal["open", "closed", "cancelled"]:
    if ev.status == EventStatus.cancelled:
        return "cancelled"
    if ev.status == EventStatus.closed or (
        ev.registration_deadline is not None and ev.registration_deadline < now
    ):
        return "closed"
    return "open"


def _via(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host.removeprefix("www.")


def _source(s: EventSource) -> SourceOut:
    return SourceOut(
        url=s.source_url,
        snippet=plain_text(s.snippet),
        found_at=s.created_at,
        via=_via(s.source_url),
    )


def _event_fields(row: Any, sources: list[SourceOut], now: datetime) -> dict[str, Any]:
    ev: Event = row.Event
    org: Organisation | None = row.Organisation
    return {
        "id": ev.id,
        "title": plain_text(ev.title),
        "organiser": OrganiserOut(
            name=plain_text(org.name if org else ev.organiser_name),
            id=org.id if org else None,
            kind=org.kind if org else None,
            tier=org.tier if org else None,
            region=org.region if org else None,
        ),
        "registration_url": ev.registration_url,
        "registration_deadline": ev.registration_deadline,
        "event_start": ev.event_start,
        "event_end": ev.event_end,
        "mode": ev.mode,
        "location": plain_text(ev.location) if ev.location else None,
        "eligibility": [plain_text(e) for e in ev.eligibility],
        "status": _effective_status(ev, now),
        "review_status": ev.review_status,
        "needs_review": ev.review_status == ReviewStatus.needs_review,
        "deadline_changed_at": ev.deadline_changed_at,
        "is_new": bool(row.is_new),
        "is_registered": bool(row.is_registered),
        "is_tracked": bool(row.is_tracked),
        "sources": sources,
    }


async def _sources(
    db: AsyncSession, event_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[SourceOut]]:
    out: dict[uuid.UUID, list[SourceOut]] = {i: [] for i in event_ids}
    if event_ids:
        rows = await db.scalars(
            select(EventSource)
            .where(EventSource.event_id.in_(event_ids))
            .order_by(EventSource.created_at, EventSource.id)
        )
        for s in rows:
            out[s.event_id].append(_source(s))
    return out


# --- endpoints ---------------------------------------------------------------------------


@router.get("/events")
async def list_events(  # role: any (doc 07 §3)
    user: AnyUser,
    db: Db,
    kind: str | None = None,
    tier: str | None = None,
    region: str | None = None,
    mode: str | None = None,
    status: str | None = None,
    deadline_before: datetime | None = None,
    deadline_after: datetime | None = None,
    registered: bool | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    new_only: bool = False,
    cursor: Annotated[str | None, Query(max_length=500)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> EventPage:
    now = _now()
    stmt = _base(user)
    statuses: list[FeedStatus] = _csv(status, _parse_status, "status") or ["open"]
    stmt = stmt.where(or_(*(_status_clause(s, now) for s in statuses)))
    kinds: list[OrgKind] | None = _csv(kind, OrgKind, "kind")
    if kinds:
        stmt = stmt.where(Organisation.kind.in_(kinds))
    tiers: list[int] | None = _csv(tier, _parse_tier, "tier")
    if tiers:
        stmt = stmt.where(Organisation.tier.in_(tiers))
    regions: list[OrgRegion] | None = _csv(region, OrgRegion, "region")
    if regions:
        stmt = stmt.where(_region_expr().in_([r.value for r in regions]))
    modes: list[EventMode] | None = _csv(mode, EventMode, "mode")
    if modes:
        stmt = stmt.where(Event.mode.in_(modes))
    if (before := _utc(deadline_before)) is not None:
        stmt = stmt.where(Event.registration_deadline < before)
    if (after := _utc(deadline_after)) is not None:
        stmt = stmt.where(Event.registration_deadline >= after)
    if registered is not None:
        mine = _mine(Registration, user)
        stmt = stmt.where(mine if registered else ~mine)
    if q and q.strip():
        pattern = _like(q.strip())
        stmt = stmt.where(or_(Event.title.ilike(pattern), Event.organiser_name.ilike(pattern)))
    if new_only:
        stmt = stmt.where(_is_new(user))
    if cursor:
        stmt = stmt.where(_after_cursor(cursor))
    stmt = stmt.order_by(Event.registration_deadline.asc().nulls_last(), Event.id).limit(limit + 1)

    rows = list((await db.execute(stmt)).all())
    page, more = rows[:limit], len(rows) > limit
    sources = await _sources(db, [r.Event.id for r in page])
    return EventPage(
        items=[EventOut(**_event_fields(r, sources[r.Event.id], now)) for r in page],
        next_cursor=_encode_cursor(page[-1].Event) if more else None,
    )


async def _visible_event(db: AsyncSession, event_id: uuid.UUID) -> Event:
    ev = await db.scalar(select(Event).where(Event.id == event_id, _visible()))
    if ev is None:
        raise ApiError(404, "not_found", "Not found.")
    return ev


@router.get("/events/{event_id}")
async def get_event(event_id: uuid.UUID, user: AnyUser, db: Db) -> EventDetailOut:  # role: any
    row = (await db.execute(_base(user).where(Event.id == event_id))).first()
    if row is None:
        raise ApiError(404, "not_found", "Not found.")
    sources = await _sources(db, [event_id])
    ev: Event = row.Event
    return EventDetailOut(
        **_event_fields(row, sources[event_id], _now()),
        description=plain_text(ev.description),
        prize=plain_text(ev.prize) if ev.prize else None,
    )


async def recompute_reminders(db: AsyncSession, user_id: uuid.UUID, event_id: uuid.UUID) -> None:
    """Rebuild this user's pending reminders for this event in the caller's transaction
    (registered -> event-date offsets, tracked -> deadline offsets, neither -> none)."""
    await db.flush()  # the registration / track change must be visible to the planner
    await reminders.plan(db, user_id, [event_id])


async def _set_interest(
    db: AsyncSession,
    model: type[Registration] | type[Track],
    user: User,
    event_id: uuid.UUID,
    on: bool,
) -> None:
    await _visible_event(db, event_id)
    if on:
        await db.execute(
            insert(model)
            .values(user_id=user.id, event_id=event_id)
            .on_conflict_do_nothing(index_elements=["user_id", "event_id"])
        )
    else:  # ownership: only ever the caller's own row
        await db.execute(delete(model).where(model.user_id == user.id, model.event_id == event_id))
    await recompute_reminders(db, user.id, event_id)
    await db.commit()


@router.post("/events/{event_id}/registration")
async def register(event_id: uuid.UUID, user: AnyUser, db: Db) -> RegistrationOut:  # role: any
    await _set_interest(db, Registration, user, event_id, True)
    return RegistrationOut(is_registered=True)


@router.delete("/events/{event_id}/registration")
async def unregister(event_id: uuid.UUID, user: AnyUser, db: Db) -> RegistrationOut:  # role: any
    await _set_interest(db, Registration, user, event_id, False)
    return RegistrationOut(is_registered=False)


@router.post("/events/{event_id}/track")
async def track(event_id: uuid.UUID, user: AnyUser, db: Db) -> TrackOut:  # role: any
    await _set_interest(db, Track, user, event_id, True)
    return TrackOut(is_tracked=True)


@router.delete("/events/{event_id}/track")
async def untrack(event_id: uuid.UUID, user: AnyUser, db: Db) -> TrackOut:  # role: any
    await _set_interest(db, Track, user, event_id, False)
    return TrackOut(is_tracked=False)


@router.get("/me/unread-count")
async def unread_count(user: AnyUser, db: Db) -> UnreadCount:  # role: any, own rows only
    new_events = await db.scalar(
        select(func.count())
        .select_from(Event)
        .where(_visible(), _status_clause("open", _now()), _is_new(user))
    )
    notifications = await db.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user.id, Notification.read_at.is_(None))
    )
    return UnreadCount(new_events=new_events or 0, notifications=notifications or 0)


@router.post("/me/seen")
async def seen(user: AnyUser, db: Db) -> SeenOut:  # role: any, own row only
    last_seen_at = await db.scalar(
        update(User)
        .where(User.id == user.id)
        .values(last_seen_at=func.now())
        .returning(User.last_seen_at)
    )
    await db.commit()
    assert last_seen_at is not None
    return SeenOut(last_seen_at=last_seen_at)
