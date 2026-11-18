"""Admin: what changed in the feed, and a manual "Refresh now" (owner decision 2026-11-19).

- `GET /admin/changes?hours=24`: crawl status (last success, next due run) plus recent
  changes, newest first: `new` (first seen), `deadline_changed` (a source moved the
  deadline, HE-072) and `cancelled`. Each item says whether it is a priority event
  (Fortune company / tier 1 college, common.org_match.is_priority), which is what the feed
  shows by default. Rejected events are left out.
- `POST /admin/refresh`: makes every enabled source due now (the per-minute scheduler cron
  picks them up, so politeness and SKIP LOCKED still apply) and re-links unmatched events
  to organisations. Once per REFRESH_COOLDOWN_S for everyone, so a double click or two
  admins cannot hammer the crawled sites.

Admin + 2FA on every route (router dependency, doc 07 §3). Scraped text goes out as plain text.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db, get_redis
from api.errors import ApiError
from api.events import plain_text
from api.totp import require_admin_2fa
from common import org_match
from common.models import Event, EventStatus, Organisation, ReviewStatus, Source

router = APIRouter(prefix="/api/v1/admin", dependencies=[Depends(require_admin_2fa)])
Db = Annotated[AsyncSession, Depends(get_db)]
Rds = Annotated[Redis, Depends(get_redis)]

REFRESH_COOLDOWN_S = 600
REFRESH_KEY = "admin:refresh_all"
CHANGES_MAX = 200

Change = Literal["new", "deadline_changed", "cancelled"]


class ChangeOut(BaseModel):
    event_id: uuid.UUID
    title: str
    organiser: str
    change: Change
    at: datetime
    registration_deadline: datetime | None
    priority: bool


class CrawlStatus(BaseModel):
    sources_enabled: int
    last_success_at: datetime | None
    next_run_at: datetime | None
    refresh_available_at: datetime | None  # null = "Refresh now" may be pressed


class ChangesOut(BaseModel):
    crawl: CrawlStatus
    items: list[ChangeOut]


class RefreshOut(BaseModel):
    sources_queued: int
    events_linked: int
    refresh_available_at: datetime


async def _available_at(redis: Redis) -> datetime | None:
    ttl = int(await redis.ttl(REFRESH_KEY))
    return datetime.now(UTC) + timedelta(seconds=ttl) if ttl > 0 else None


@router.get("/changes")
async def changes(
    db: Db, redis: Rds, hours: Annotated[int, Query(ge=1, le=168)] = 24
) -> ChangesOut:  # role: admin + 2FA
    since = datetime.now(UTC) - timedelta(hours=hours)
    crawl = (
        await db.execute(
            select(
                func.count(),
                func.max(Source.last_success_at),
                func.min(Source.next_run_at),
            ).where(Source.is_enabled)
        )
    ).one()
    rows = (
        await db.execute(
            select(Event, Organisation.name, org_match.is_priority().label("priority"))
            .outerjoin(Organisation, Organisation.id == Event.organisation_id)
            .where(
                Event.review_status != ReviewStatus.rejected,
                or_(
                    Event.first_seen_at >= since,
                    Event.deadline_changed_at >= since,
                    and_(Event.status == EventStatus.cancelled, Event.updated_at >= since),
                ),
            )
            .order_by(Event.updated_at.desc())
            .limit(CHANGES_MAX)
        )
    ).all()
    items: list[ChangeOut] = []
    for ev, org_name, priority in rows:
        found: list[tuple[Change, datetime | None]] = [
            ("new", ev.first_seen_at),
            ("deadline_changed", ev.deadline_changed_at),
            ("cancelled", ev.updated_at if ev.status == EventStatus.cancelled else None),
        ]
        for change, at in found:
            if at is not None and at >= since:
                items.append(
                    ChangeOut(
                        event_id=ev.id,
                        title=plain_text(ev.title),
                        organiser=plain_text(org_name or ev.organiser_name),
                        change=change,
                        at=at,
                        registration_deadline=ev.registration_deadline,
                        priority=bool(priority),
                    )
                )
    items.sort(key=lambda c: c.at, reverse=True)
    return ChangesOut(
        crawl=CrawlStatus(
            sources_enabled=crawl[0],
            last_success_at=crawl[1],
            next_run_at=crawl[2],
            refresh_available_at=await _available_at(redis),
        ),
        items=items,
    )


@router.post("/refresh", status_code=202)
async def refresh_all(db: Db, redis: Rds) -> RefreshOut:  # role: admin + 2FA
    if not await redis.set(REFRESH_KEY, "1", nx=True, ex=REFRESH_COOLDOWN_S):
        ttl = int(await redis.ttl(REFRESH_KEY))
        raise ApiError(
            429,
            "rate_limited",
            "A refresh is already running. Try again in a few minutes.",
            headers={"Retry-After": str(max(ttl, 1))},
        )
    conn = await db.connection()
    queued = (
        await conn.execute(text("UPDATE sources SET next_run_at = now() WHERE is_enabled"))
    ).rowcount
    linked = await org_match.link_unmatched(conn)
    await db.commit()
    return RefreshOut(
        sources_queued=queued,
        events_linked=linked,
        refresh_available_at=datetime.now(UTC) + timedelta(seconds=REFRESH_COOLDOWN_S),
    )
