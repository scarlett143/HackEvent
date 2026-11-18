"""Reminder planner (HE-070, TRD §4.2, PRD §4.5).

`plan(db, user_id, event_ids=None)` recomputes the user's *pending* reminders inside the
caller's transaction (register / track toggles, prefs / timezone / quiet-hours changes;
`replan_event` when an event's dates or status move, HE-072):

- registered event -> kind `event`, anchored on `event_start` (event offsets);
  tracked ("Remind me"), not registered, still open -> kind `deadline`, anchored on
  `registration_deadline` (deadline offsets). Cancelled / rejected events get none;
- one row per (offset, enabled channel), fired at `send_hour_local` in the user's
  timezone `offset` days before the anchor's local date. A slot at or after the anchor
  (0-day, early deadline) moves to 1 h before it; quiet hours push it to the window's
  end; slots already in the past are not planned;
- rows are keyed (user, event, kind, offset_days, channel) (AGENTS.md: keep this key).
  Wanted rows are upserted and only a *pending* row is ever updated, so a sent / failed /
  cancelled reminder is never re-armed: each reminder can be sent at most once. Pending
  rows that are no longer wanted are deleted ("delete pending, recompute", TRD §4.2.5).
"""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from common.models import (
    Channel,
    Event,
    EventStatus,
    NotificationPrefs,
    Registration,
    Reminder,
    ReminderKind,
    ReminderStatus,
    ReviewStatus,
    Track,
    User,
)

DEFAULT_OFFSETS = [3, 2, 1, 0]  # PRD §4.5: 3, 2, 1, 0 days by default
DEFAULT_CHANNELS = {"email": True, "in_app": True}
DEFAULT_SEND_HOUR = 9
DEFAULT_TZ = "Asia/Kolkata"
LATE_SLOT_LEAD = timedelta(hours=1)  # a slot past the anchor fires this long before it

Key = tuple[uuid.UUID, ReminderKind, int, Channel]  # (event, kind, offset, channel)


@dataclass(frozen=True)
class Prefs:
    deadline_offsets: Sequence[int]
    event_offsets: Sequence[int]
    send_hour: int
    channels: tuple[Channel, ...]
    tz: ZoneInfo
    quiet_start: time | None
    quiet_end: time | None


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def prefs_for(user: User, row: NotificationPrefs | None) -> Prefs:
    enabled = DEFAULT_CHANNELS | (row.channels if row else {})
    return Prefs(
        deadline_offsets=row.deadline_offsets_days if row else DEFAULT_OFFSETS,
        event_offsets=row.event_offsets_days if row else DEFAULT_OFFSETS,
        send_hour=row.send_hour_local if row else DEFAULT_SEND_HOUR,
        channels=tuple(c for c in Channel if enabled.get(c.value)),
        tz=_zone(user.timezone),
        quiet_start=user.quiet_start,
        quiet_end=user.quiet_end,
    )


def after_quiet(local: datetime, start: time | None, end: time | None) -> datetime:
    """Move a local time inside the quiet window [start, end) to the window's end."""
    if start is None or end is None or start == end:
        return local
    t, day = local.time(), local.date()
    if start < end:
        inside, end_day = start <= t < end, day
    else:  # wraps midnight, e.g. 22:00-07:00
        inside = t >= start or t < end
        end_day = day + timedelta(days=1) if t >= start else day
    if not inside:
        return local
    return datetime.combine(end_day, end, tzinfo=local.tzinfo)


def slot(anchor: datetime, offset_days: int, p: Prefs) -> datetime:
    """When the `offset_days` reminder for `anchor` fires (UTC)."""
    day: date = anchor.astimezone(p.tz).date() - timedelta(days=offset_days)
    local = datetime.combine(day, time(p.send_hour), tzinfo=p.tz)
    if local >= anchor:
        local = (anchor - LATE_SLOT_LEAD).astimezone(p.tz)
    return after_quiet(local, p.quiet_start, p.quiet_end).astimezone(UTC)


def wanted(
    events: Iterable[tuple[Event, bool, bool]], p: Prefs, now: datetime
) -> dict[Key, datetime]:
    """(event, registered, tracked) -> {(event, kind, offset, channel): scheduled_at}."""
    out: dict[Key, datetime] = {}
    for ev, registered, tracked in events:
        if ev.status == EventStatus.cancelled or ev.review_status == ReviewStatus.rejected:
            continue
        if registered:
            kind, anchor, offsets = ReminderKind.event, ev.event_start, p.event_offsets
        elif tracked and ev.status == EventStatus.open:
            kind, anchor = ReminderKind.deadline, ev.registration_deadline
            offsets = p.deadline_offsets
        else:
            continue
        if anchor is None or anchor <= now:
            continue
        for offset in set(offsets):
            at = slot(anchor, offset, p)
            if at <= now:
                continue
            for channel in p.channels:
                out[(ev.id, kind, offset, channel)] = at
    return out


async def plan(
    db: AsyncSession,
    user_id: uuid.UUID,
    event_ids: Sequence[uuid.UUID] | None = None,
    *,
    now: datetime | None = None,
) -> int:
    """Recompute pending reminders for one user (all their events, or just `event_ids`).
    Runs in the caller's transaction; returns the number of reminders now wanted."""
    now = now or datetime.now(UTC)
    user = await db.get(User, user_id)
    if user is None:
        return 0
    row = await db.scalar(select(NotificationPrefs).where(NotificationPrefs.user_id == user_id))
    p = prefs_for(user, row)

    registered = select(Registration.event_id).where(Registration.user_id == user_id)
    tracked = select(Track.event_id).where(Track.user_id == user_id)
    q = select(
        Event,
        Event.id.in_(registered).label("registered"),
        Event.id.in_(tracked).label("tracked"),
    ).where(or_(Event.id.in_(registered), Event.id.in_(tracked)))
    if event_ids is not None:
        q = q.where(Event.id.in_(event_ids))
    rows = (await db.execute(q)).all() if user.is_active else []
    want = wanted(((r.Event, r.registered, r.tracked) for r in rows), p, now)

    stale = delete(Reminder).where(
        Reminder.user_id == user_id, Reminder.status == ReminderStatus.pending
    )
    if event_ids is not None:
        stale = stale.where(Reminder.event_id.in_(event_ids))
    if want:
        cols = tuple_(Reminder.event_id, Reminder.kind, Reminder.offset_days, Reminder.channel)
        stale = stale.where(cols.not_in(list(want)))
    await db.execute(stale.execution_options(synchronize_session=False))

    if want:
        ins = insert(Reminder).values(
            [
                {
                    "user_id": user_id,
                    "event_id": e,
                    "kind": k,
                    "offset_days": o,
                    "channel": c,
                    "scheduled_at": at,
                    "status": ReminderStatus.pending,
                }
                for (e, k, o, c), at in want.items()
            ]
        )
        await db.execute(
            ins.on_conflict_do_update(
                index_elements=["user_id", "event_id", "kind", "offset_days", "channel"],
                set_={"scheduled_at": ins.excluded.scheduled_at, "updated_at": func.now()},
                where=Reminder.status == ReminderStatus.pending,
            )
        )
    return len(want)


async def replan_event(
    db: AsyncSession, event_id: uuid.UUID, *, now: datetime | None = None
) -> int:
    """HE-072: an event's dates or status changed -> replan it for every user who
    registered or tracked it (TRD §4.2.5: pending replaced, sent kept). Same transaction
    as the change. Returns the number of users replanned."""
    users = (
        await db.scalars(
            select(Registration.user_id)
            .where(Registration.event_id == event_id)
            .union(select(Track.user_id).where(Track.event_id == event_id))
        )
    ).all()
    for user_id in users:
        await plan(db, user_id, [event_id], now=now)
    return len(users)
