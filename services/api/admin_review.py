"""Admin review queue (HE-093, App Flow F11, doc 06 §A7).

- `GET /admin/review`: `needs_review` events, oldest first, each with its sources
  (plain-text snippet, extractor, review reasons from extraction) and up to 3 likely
  duplicates (pg_trgm title similarity) to merge into;
- `POST /admin/review/{event_id}` `{action: approve | reject | merge, merge_into?, patch?}`:
  - approve (optionally "Edit + approve" with `patch`): fields replaced, review_status
    approved; a new deadline stamps `deadline_changed_at` (HE-072 chip);
  - reject: review_status rejected (hidden from every feed, reminders dropped);
  - merge: sources, registrations, tracks, calendar items and notifications move to
    `merge_into`; the duplicate is deleted.
  Every decision is audited (`review.decision`) and replans the affected reminders
  (HE-072) in the same transaction. Only `needs_review` events can be decided (409).

Admin + 2FA on every route (router dependency, doc 07 §3).
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import AfterValidator, BaseModel, Field, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import Auth
from api.deps import get_db
from api.errors import ApiError
from api.events import plain_text
from api.invites import _ip
from api.totp import require_admin_2fa
from common import reminders
from common.audit import AuditAction, record
from common.models import Event, EventMode, EventSource, EventStatus, ReviewStatus
from workers.scrape.adapters.base import safe_url

router = APIRouter(prefix="/api/v1/admin", dependencies=[Depends(require_admin_2fa)])
Db = Annotated[AsyncSession, Depends(get_db)]
AdminAuth = Annotated[Auth, Depends(require_admin_2fa)]

QUEUE_MAX = 50
DUPLICATES_MAX = 3
SIMILAR_MIN = 0.3  # pg_trgm similarity floor for "possible duplicate" suggestions
SNIPPET_MAX = 600


class SourceOut(BaseModel):
    url: str
    snippet: str
    extractor: str
    found_at: datetime
    reasons: list[str]


class DuplicateOut(BaseModel):
    id: uuid.UUID
    title: str
    organiser: str
    registration_deadline: datetime | None
    event_start: datetime | None
    similarity: float


class ReviewItem(BaseModel):
    id: uuid.UUID
    title: str
    organiser: str
    registration_url: str
    registration_deadline: datetime | None
    event_start: datetime | None
    event_end: datetime | None
    mode: EventMode
    location: str | None
    confidence: float
    first_seen_at: datetime
    sources: list[SourceOut]
    duplicates: list[DuplicateOut]


class ReviewQueue(BaseModel):
    items: list[ReviewItem]
    total: int


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("datetimes must include a timezone offset")
    return value.astimezone(UTC) if value else None


def _http_url(value: str) -> str:
    url = safe_url(value)
    if url is None:
        raise ValueError("registration_url must be an http(s) URL")
    return url


Aware = Annotated[datetime | None, AfterValidator(_aware)]


class Patch(BaseModel):
    """'Edit + approve'. Only the fields sent are changed; deadline and event dates stay
    separate fields (AGENTS.md)."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    registration_url: Annotated[str, AfterValidator(_http_url)] | None = None
    registration_deadline: Aware = None
    event_start: Aware = None
    event_end: Aware = None
    mode: EventMode | None = None
    location: str | None = Field(default=None, max_length=200)


class DecisionIn(BaseModel):
    action: Literal["approve", "reject", "merge"]
    merge_into: uuid.UUID | None = None
    patch: Patch | None = None

    @model_validator(mode="after")
    def _shape(self) -> "DecisionIn":
        if (self.action == "merge") != (self.merge_into is not None):
            raise ValueError("merge_into is required for merge, and only for merge")
        if self.patch is not None and self.action != "approve":
            raise ValueError("patch is only allowed with approve")
        return self


class DecisionOut(BaseModel):
    id: uuid.UUID
    action: str
    review_status: ReviewStatus | None  # None: merged away (deleted)


def _reasons(extracted: dict[str, Any] | None) -> list[str]:
    raw = (extracted or {}).get("review_reasons") or []
    return [str(r) for r in raw if isinstance(r, str)][:10]


async def _duplicates(db: AsyncSession, ev: Event) -> list[DuplicateOut]:
    rows = await db.execute(
        text(
            "SELECT id, title, organiser_name, registration_deadline, event_start,"
            " similarity(lower(title), lower(:t)) AS s FROM events"
            " WHERE id <> :id AND review_status <> 'rejected'"
            " AND similarity(lower(title), lower(:t)) >= :min"
            " ORDER BY s DESC, first_seen_at LIMIT :n"
        ),
        {"t": ev.title, "id": ev.id, "min": SIMILAR_MIN, "n": DUPLICATES_MAX},
    )
    return [
        DuplicateOut(
            id=r.id,
            title=r.title,
            organiser=r.organiser_name,
            registration_deadline=r.registration_deadline,
            event_start=r.event_start,
            similarity=round(float(r.s), 2),
        )
        for r in rows
    ]


@router.get("/review")
async def review_queue(db: Db) -> ReviewQueue:  # role: admin + 2FA
    pending = Event.review_status == ReviewStatus.needs_review
    events = (
        await db.scalars(
            select(Event).where(pending).order_by(Event.first_seen_at, Event.id).limit(QUEUE_MAX)
        )
    ).all()
    total = await db.scalar(select(func.count()).select_from(Event).where(pending)) or 0
    items = []
    for ev in events:
        sources = (
            await db.scalars(
                select(EventSource)
                .where(EventSource.event_id == ev.id)
                .order_by(EventSource.created_at)
            )
        ).all()
        items.append(
            ReviewItem(
                id=ev.id,
                title=ev.title,
                organiser=ev.organiser_name,
                registration_url=ev.registration_url,
                registration_deadline=ev.registration_deadline,
                event_start=ev.event_start,
                event_end=ev.event_end,
                mode=ev.mode,
                location=ev.location,
                confidence=float(ev.confidence),
                first_seen_at=ev.first_seen_at,
                sources=[
                    SourceOut(
                        url=s.source_url,
                        snippet=plain_text(s.snippet)[:SNIPPET_MAX],
                        extractor=s.extractor,
                        found_at=s.created_at,
                        reasons=_reasons(s.extracted),
                    )
                    for s in sources
                ],
                duplicates=await _duplicates(db, ev),
            )
        )
    return ReviewQueue(items=items, total=total)


async def _merge(db: AsyncSession, dup: Event, target_id: uuid.UUID) -> None:
    """Move everything that points at `dup` onto `target_id`, then delete `dup`."""
    p = {"dup": dup.id, "to": target_id}
    await db.execute(
        text(
            "UPDATE event_sources s SET event_id = :to WHERE s.event_id = :dup AND NOT EXISTS"
            " (SELECT 1 FROM event_sources t WHERE t.event_id = :to"
            " AND t.source_url = s.source_url)"
        ),
        p,
    )
    for table in ("registrations", "tracks"):
        await db.execute(
            text(
                f"INSERT INTO {table} (user_id, event_id) SELECT user_id, :to FROM {table}"
                " WHERE event_id = :dup ON CONFLICT (user_id, event_id) DO NOTHING"
            ),
            p,
        )
    for table in ("calendar_items", "notifications"):
        await db.execute(text(f"UPDATE {table} SET event_id = :to WHERE event_id = :dup"), p)
    await db.delete(dup)  # cascades its leftover sources / registrations / tracks / reminders


@router.post("/review/{event_id}")
async def decide(
    event_id: uuid.UUID, body: DecisionIn, request: Request, auth: AdminAuth, db: Db
) -> DecisionOut:  # role: admin + 2FA
    ev = await db.scalar(select(Event).where(Event.id == event_id).with_for_update())
    if ev is None:
        raise ApiError(404, "not_found", "Not found.")
    if ev.review_status != ReviewStatus.needs_review:
        raise ApiError(409, "already_decided", "This event was already reviewed.")
    meta: dict[str, Any] = {"action": body.action}
    now = datetime.now(UTC)

    if body.action == "merge":
        assert body.merge_into is not None
        target = await db.scalar(
            select(Event)
            .where(Event.id == body.merge_into, Event.review_status != ReviewStatus.rejected)
            .with_for_update()
        )
        if target is None or target.id == ev.id:
            raise ApiError(422, "invalid_target", "Pick another, not rejected event to merge into.")
        meta |= {"merge_into": str(target.id), "title": ev.title}
        await _merge(db, ev, target.id)
        await db.flush()
        await reminders.replan_event(db, target.id)
        out = DecisionOut(id=event_id, action=body.action, review_status=None)
    else:
        if body.action == "approve":
            changes = body.patch.model_dump(exclude_unset=True) if body.patch else {}
            for field in ("title", "registration_url", "mode"):
                if changes.get(field, "") is None:
                    changes.pop(field)  # required columns: never set to null
            if changes.get("registration_deadline", ev.registration_deadline) != (
                ev.registration_deadline
            ):
                ev.deadline_changed_at = now
            for field, value in changes.items():
                setattr(ev, field, value)
            if ev.status != EventStatus.cancelled:
                d = ev.registration_deadline
                ev.status = EventStatus.closed if d and d < now else EventStatus.open
            ev.review_status = ReviewStatus.approved
            meta["fields"] = sorted(changes)
        else:
            ev.review_status = ReviewStatus.rejected
        await db.flush()
        await reminders.replan_event(db, ev.id)
        out = DecisionOut(id=ev.id, action=body.action, review_status=ev.review_status)

    await record(
        db,
        action=AuditAction.REVIEW_DECISION,
        actor_id=auth.user.id,
        target_type="event",
        target_id=event_id,
        ip=_ip(request),
        meta=meta,
    )
    await db.commit()
    return out
