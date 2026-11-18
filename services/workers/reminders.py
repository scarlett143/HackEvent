"""Reminder dispatcher (HE-071, TRD §4.2 steps 2-4, App Flow F7).

- `dispatch_due` (scheduler cron, every minute): due pending reminders, `FOR UPDATE SKIP
  LOCKED`, are enqueued as `send_reminder(id)` on the notify queue with job id
  `reminder:<id>:<attempts>`, so a reminder is never queued twice for the same attempt
  (arq keeps a finished job id for an hour; the attempt count lets a retry through).
- `send_reminder` (worker-notify) claims the row *before* delivering: one UPDATE flips
  it pending -> sent only if it is still pending and due. A crash after the claim loses
  that one message rather than sending it twice (PRD: "Reminders sent twice: 0").
  Delivery goes through the channel's notifier plugin (common.notifiers, HE-073):
  - in_app: the notification row is written in the same transaction as the claim;
  - others (email today): after the claim commits; a failed SendResult puts the row back
    to pending with backoff, and after MAX_ATTEMPTS (or `permanent_failure`) it becomes
    `failed` with the error;
  - channels without a plugin yet (fcm, telegram, ntfy, gcal): `failed`, reason given.
  A reminder whose user is inactive or whose event no longer qualifies (cancelled,
  rejected, deadline passed / registration closed) is `cancelled`, not sent.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text

from common import notifiers
from common.notifiers import Endpoint, Message
from common.queues import NOTIFY_QUEUE
from common.reminders import DEFAULT_TZ
from workers import scheduler

log = logging.getLogger(__name__)

BATCH = 200  # ponytail: 200/min ceiling; ~10 users x a few events is far below it
MAX_ATTEMPTS = 5
BACKOFF = timedelta(minutes=5)  # x attempts: 5, 10, 15, 20 min
ERROR_MAX = 500


async def dispatch_due(ctx: dict[str, Any], *, now: datetime | None = None) -> int:
    """Enqueue every due pending reminder; returns how many were handed over."""
    async with scheduler.db_engine(ctx).begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT id::text, attempts FROM reminders WHERE status = 'pending'"
                    " AND scheduled_at <= :now ORDER BY scheduled_at LIMIT :n"
                    " FOR UPDATE SKIP LOCKED"
                ),
                {"now": now or datetime.now(UTC), "n": BATCH},
            )
        ).all()
        for rid, attempts in rows:
            await ctx["redis"].enqueue_job(
                "send_reminder",
                rid,
                _job_id=f"reminder:{rid}:{attempts}",
                _queue_name=NOTIFY_QUEUE,
            )
    return len(rows)


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def local_when(at: datetime, tz: ZoneInfo) -> str:
    """'3 Dec 2026, 11:59 PM (Asia/Kolkata)'."""
    local = at.astimezone(tz)
    return f"{local.day} {local:%b %Y}, {local:%I:%M %p}".replace(" 0", " ") + f" ({tz.key})"


def _days_word(days: int) -> str:
    return "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"


def message(r: Any, now: datetime) -> dict[str, str]:
    """Headline / when line for reminder row `r` (App Flow F7: "2 days left to register")."""
    tz = zone(r.timezone)
    anchor: datetime = r.registration_deadline if r.kind == "deadline" else r.event_start
    days = max(0, (anchor.astimezone(tz).date() - now.astimezone(tz).date()).days)
    if r.kind == "deadline":
        headline = (
            "Last day to register"
            if days == 0
            else f"{days} day{'s' if days != 1 else ''} left to register"
        )
        when_label = "Registration closes"
    else:
        headline = f"Starts {_days_word(days)}"
        when_label = "Starts"
    return {
        "headline": headline,
        "title": " ".join(r.title.split()),
        "when_label": when_label,
        "when": local_when(anchor, tz),
        "path": f"/events/{r.event_id}",
    }


def _still_wanted(r: Any, now: datetime) -> bool:
    if not r.is_active or r.event_status == "cancelled" or r.review_status == "rejected":
        return False
    if r.kind == "deadline":
        deadline = r.registration_deadline
        return bool(r.event_status == "open" and deadline is not None and deadline > now)
    return bool(r.event_start is not None and r.event_start > now)


async def send_reminder(
    ctx: dict[str, Any], reminder_id: str, *, now: datetime | None = None
) -> str:
    """Returns sent | skipped | cancelled | retry | failed."""
    now = now or datetime.now(UTC)
    engine = scheduler.db_engine(ctx)
    async with engine.begin() as conn:
        r = (
            await conn.execute(
                text(
                    "UPDATE reminders r SET status = 'sent', sent_at = :now,"
                    " attempts = r.attempts + 1, updated_at = now()"
                    " FROM users u, events e"
                    " WHERE r.id = CAST(:id AS uuid) AND r.status = 'pending'"
                    " AND r.scheduled_at <= :now AND u.id = r.user_id AND e.id = r.event_id"
                    " RETURNING r.id, r.user_id, r.event_id, r.kind::text AS kind,"
                    " r.channel::text AS channel, r.attempts, u.email, u.timezone, u.is_active,"
                    " e.title, e.registration_deadline, e.event_start,"
                    " e.status::text AS event_status, e.review_status::text AS review_status"
                ),
                {"id": reminder_id, "now": now},
            )
        ).first()
        if r is None:
            return "skipped"  # already handled, replanned later, or deleted
        if not _still_wanted(r, now):
            await _set(conn, r.id, "cancelled", None)
            return "cancelled"
        msg = Message(**message(r, now), event_id=r.event_id)
        notifier = notifiers.build(r.channel, ctx, conn)
        if notifier is None:
            await _set(conn, r.id, "failed", f"no sender for channel {r.channel} yet")
            return "failed"
        if r.channel == "in_app":  # delivered inside the claim transaction
            result = await notifier.send(Endpoint(r.user_id), msg)
            if result.ok:
                return "sent"
            await _set(conn, r.id, "failed", result.error)
            return "failed"
    # Other channels deliver after the claim is committed (never inside a DB transaction).
    result = await notifier.send(Endpoint(r.user_id, r.email), msg)
    if result.ok:
        return "sent"
    permanent = result.permanent_failure or r.attempts >= MAX_ATTEMPTS
    error = (result.error or "send failed")[:ERROR_MAX]
    async with engine.begin() as conn:
        if permanent:
            await _set(conn, r.id, "failed", error)
        else:
            await conn.execute(
                text(
                    "UPDATE reminders SET status = 'pending', sent_at = NULL,"
                    " scheduled_at = :at, last_error = :err, updated_at = now()"
                    " WHERE id = :id"
                ),
                {"id": r.id, "at": now + BACKOFF * r.attempts, "err": error},
            )
    log.warning(
        "reminder.send_failed id=%s channel=%s attempt=%d permanent=%s",
        r.id,
        r.channel,
        r.attempts,
        permanent,
    )
    return "failed" if permanent else "retry"


async def _set(conn: Any, rid: uuid.UUID, status: str, error: str | None) -> None:
    """Terminal state: sent_at cleared (it was set by the claim)."""
    await conn.execute(
        text(
            "UPDATE reminders SET status = CAST(:s AS reminder_status), sent_at = NULL,"
            " last_error = :err, updated_at = now() WHERE id = :id"
        ),
        {"id": rid, "s": status, "err": error},
    )
