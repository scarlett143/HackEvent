"""New-event alerts by user filters (HE-078, doc 05 notification_prefs, doc 06 B3).

When is an event "new"? When it first becomes visible as a trusted event:
- dedupe created it with review_status `auto` (workers.ingest, outcome "created"), or
- an admin approved it from the review queue (api.admin_review).
A `needs_review` event alerts nobody until it is approved; a rejected one never. Closed
(deadline passed) and cancelled events alert nobody.

Who? Every active user whose `new_event_alerts` is on (default on, also with no prefs
row) and whose `new_event_filters` match. Filters are `{"region": [...], "kind": [...],
"mode": [...]}`; a missing or empty key means "any". Each key works like the feed filter
of the same name (api/events.py):
- region: `region_expr()`, shared with the feed. The matched organisation's region; with
  no matched organisation (most scraped events): "india" if the location names India or
  a source is on Unstop, otherwise "global";
- kind: the matched organisation's kind. An event with no matched organisation has no
  kind, so a kind filter does not match it (the feed's `kind=` hides it too);
- mode: the event's mode (`unknown` matches only a filter that lists it).

How? Through the notifier plugins' channels in the user's prefs (DEFAULT_CHANNELS
merged with `channels`): in_app writes the inbox row with InAppNotifier on the caller's
connection (same transaction as the trigger); email returns an `EmailAlert` that the
caller queues after commit (`enqueue`) as `send_email(..., "new_event")` on worker-notify,
job id `new_event:<event>:<user>`.

Once per event per user: the trigger itself happens once per event (the dedupe insert
wins once per key; approve is allowed only from needs_review, under FOR UPDATE). On top,
in-app is skipped when the user already has a `new_event` notification for the event, and
email is claimed with Redis `SET NX new_event:<event>:<user>` (EMAIL_CLAIM_TTL) before it
is queued, so even a repeated call never sends a second email (the user may have in-app
off, so there is no inbox row to check). ponytail: at-most-once: a crash between the
trigger's commit and the enqueue loses that alert rather than risking two.
ponytail: alerts go out immediately (quiet hours are a reminder setting); users are
scanned in Python, fine for the PRD's 10 users, push the filter into SQL if that grows.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import String, and_, case, cast, exists, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from common.models import (
    Event,
    EventSource,
    EventStatus,
    Notification,
    NotificationPrefs,
    Organisation,
    ReviewStatus,
    User,
)
from common.notifiers import Endpoint, InAppNotifier, Message
from common.queues import NOTIFY_QUEUE
from common.reminders import DEFAULT_CHANNELS, DEFAULT_TZ

KIND = "new_event"  # notifications.type (doc 05)
FILTER_KEYS = ("region", "kind", "mode")
EMAIL_CLAIM_TTL = 90 * 24 * 3600  # longer than any event stays "new"
# A first crawl creates hundreds of "new" events at once; without a cap every user would get
# hundreds of emails and exhaust the Gmail sending quota that reminders also need. Past the
# cap the alert is in-app only (and the feed's "New" badge still shows the event).
# A capped email is skipped for good (sending it later would defeat the cap); a user with
# in-app off gets nothing for alerts past the cap, the feed's "New" badge still shows them.
# ponytail: fixed cap; a once-a-day "N new events" summary email if users want the rest.
EMAIL_DAILY_CAP = 10


def region_expr() -> Any:
    """Event region for the India/Global filter (feed and alerts).

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


def matches(filters: dict[str, Any] | None, *, region: str, kind: str | None, mode: str) -> bool:
    """True if an event with these attributes passes the user's filters (module doc)."""
    values = {"region": region, "kind": kind, "mode": mode}
    if not isinstance(filters, dict):  # PUT /me/notification-prefs only writes dicts
        filters = {}
    for key in FILTER_KEYS:
        allowed = filters.get(key)
        if isinstance(allowed, str):
            allowed = [allowed]
        if allowed and values[key] not in allowed:
            return False
    return True


@dataclass(frozen=True)
class EmailAlert:
    user_id: uuid.UUID
    to: str
    context: dict[str, str]


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def _when(at: datetime, tz: ZoneInfo) -> str:
    """Same wording as reminder emails: '3 Dec 2026, 11:59 PM (Asia/Kolkata)'."""
    local = at.astimezone(tz)
    return f"{local.day} {local:%b %Y}, {local:%I:%M %p}".replace(" 0", " ") + f" ({tz.key})"


async def alert(
    conn: AsyncConnection, event_id: uuid.UUID, *, now: datetime | None = None
) -> list[EmailAlert]:
    """Write in-app alerts for `event_id` on `conn`; return the emails to queue after commit."""
    now = now or datetime.now(UTC)
    deadline = Event.registration_deadline
    ev = (
        await conn.execute(
            select(
                Event.title,
                Event.organiser_name,
                Event.registration_deadline,
                Event.mode,
                Organisation.kind,
                region_expr().label("region"),
            )
            .outerjoin(Organisation, Organisation.id == Event.organisation_id)
            .where(
                Event.id == event_id,
                Event.review_status.in_([ReviewStatus.auto, ReviewStatus.approved]),
                Event.status == EventStatus.open,
                or_(deadline.is_(None), deadline >= now),
            )
        )
    ).first()
    if ev is None:
        return []
    already = exists().where(
        Notification.user_id == User.id,
        Notification.event_id == event_id,
        Notification.type == KIND,
    )
    users = await conn.execute(
        select(
            User.id,
            User.email,
            User.timezone,
            NotificationPrefs.new_event_alerts,
            NotificationPrefs.new_event_filters,
            NotificationPrefs.channels,
        )
        .outerjoin(NotificationPrefs, NotificationPrefs.user_id == User.id)
        .where(and_(User.is_active, ~already))
        .order_by(User.email)
    )
    title = " ".join(ev.title.split())
    kind = ev.kind.value if ev.kind else None
    in_app = InAppNotifier(conn)
    emails: list[EmailAlert] = []
    for u in users:
        if u.new_event_alerts is False:
            continue
        if not matches(u.new_event_filters, region=ev.region, kind=kind, mode=ev.mode.value):
            continue
        channels = DEFAULT_CHANNELS | (u.channels or {})
        when = (
            _when(ev.registration_deadline, _zone(u.timezone)) if ev.registration_deadline else ""
        )
        msg = Message(
            headline="New hackathon",
            title=title,
            when_label="Registration closes",
            when=when,
            path=f"/events/{event_id}",
            event_id=event_id,
            kind=KIND,
        )
        if channels.get("in_app"):
            await in_app.send(Endpoint(u.id), msg)
        if channels.get("email"):
            context = {
                "title": title,
                "organiser": " ".join(ev.organiser_name.split()),
                "when": when or "not listed yet",
                "path": msg.path,
            }
            emails.append(EmailAlert(u.id, u.email, context))
    return emails


async def enqueue(
    redis: Any, event_id: uuid.UUID, emails: list[EmailAlert], *, now: datetime | None = None
) -> None:
    """Queue the emails `alert` returned; call only after its transaction committed.
    At most EMAIL_DAILY_CAP new-event emails per user per IST day."""
    day = (now or datetime.now(UTC)).astimezone(ZoneInfo(DEFAULT_TZ)).date().isoformat()
    for e in emails:
        key = f"new_event:{event_id}:{e.user_id}"
        if not await redis.set(key, "1", nx=True, ex=EMAIL_CLAIM_TTL):
            continue  # this user already got (or is getting) this event's email
        sent_today = f"new_event_mail:{e.user_id}:{day}"
        count = await redis.incr(sent_today)
        if count == 1:
            await redis.expire(sent_today, 2 * 24 * 3600)
        if count > EMAIL_DAILY_CAP:
            continue  # in-app alert already written; no more email today
        await redis.enqueue_job(
            "send_email",
            e.to,
            "new_event",
            e.context,
            _job_id=key,
            _queue_name=NOTIFY_QUEUE,
        )


async def notify(engine: AsyncEngine, redis: Any, event_id: uuid.UUID) -> int:
    """`alert` in its own transaction, then queue the emails. Returns emails queued."""
    async with engine.begin() as conn:
        emails = await alert(conn, event_id)
    await enqueue(redis, event_id, emails)
    return len(emails)
