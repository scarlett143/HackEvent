"""Weekly Management digest email (HE-111, PRD §2 / App Flow F9: Monday 09:00 IST).

`send_weekly_digest` runs from the scheduler cron (Monday 03:30 UTC = 09:00 IST). It sends
the dashboard summary (common.dashboard, HE-110) to every active management + admin user
(doc 07 §3 "College dashboard + digest"), one `send_email` job each on worker-notify.

Once per week: before anything is queued, Redis `SET NX` claims `digest:<ISO year>-W<week>`
(the IST calendar week) for 8 days, so a scheduler restart, a second scheduler or a manual
re-run in the same week sends nothing. The per-recipient arq job id
(`digest:<week>:<user id>`) also stops a duplicate enqueue while the job is still known.
ponytail: at-most-once, like reminders: a crash between the claim and the enqueue loses
that week's digest rather than risking two; a missed Monday (scheduler down) is not caught up.

Privacy: the email carries counts, event titles and the *names* of who registered (both
roles may see other users' registrations, doc 07 §3); never anyone's email address.
Dates are shown in each recipient's own timezone.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.dashboard import Summary, summary
from common.models import User, UserRole
from common.queues import NOTIFY_QUEUE
from common.reminders import DEFAULT_TZ
from workers import scheduler
from workers.reminders import local_when, zone

IST = ZoneInfo(DEFAULT_TZ)
CLAIM_TTL = int(timedelta(days=8).total_seconds())
KIND_LABEL = {
    "company": "Company",
    "institution": "Institution",
    "unmatched": "Not matched to an organisation",
}


def week_key(now: datetime) -> str:
    year, week, _ = now.astimezone(IST).isocalendar()
    return f"{year}-W{week:02d}"


def digest_context(s: Summary, tz: ZoneInfo) -> dict[str, Any]:
    """Template variables for one recipient (dates in their timezone)."""
    return {
        "week_of": f"{s.generated_at.astimezone(tz):%d %b %Y}".lstrip("0"),
        "open_events": s.open_events,
        "closing_this_week": s.closing_this_week,
        "new_this_week": s.new_this_week,
        "closing": [
            {
                "title": e.title,
                "organiser": e.organiser,
                "when": local_when(e.registration_deadline, tz),
                "path": f"/events/{e.id}",
                "registered": ", ".join(e.registered_by),
            }
            for e in s.closing_soon
        ],
        "people": [
            {"name": u.name, "registrations": u.registrations}
            for u in s.registrations_by_user
            if u.registrations
        ],
        "by_kind": [
            {"label": KIND_LABEL.get(k.kind, k.kind), "registrations": k.registrations}
            for k in s.registrations_by_kind
        ],
    }


async def send_weekly_digest(ctx: dict[str, Any], *, now: datetime | None = None) -> int:
    """Queue this week's digest for every recipient; returns how many (0 = already sent)."""
    now = now or datetime.now(UTC)
    week = week_key(now)
    if not await ctx["redis"].set(f"digest:{week}", now.isoformat(), nx=True, ex=CLAIM_TTL):
        return 0
    async with AsyncSession(scheduler.db_engine(ctx)) as db:
        s = await summary(db, now=now)
        recipients = (
            await db.execute(
                select(User.id, User.email, User.timezone)
                .where(User.is_active, User.role.in_([UserRole.management, UserRole.admin]))
                .order_by(User.email)
            )
        ).all()
    for uid, email, tz in recipients:
        await ctx["redis"].enqueue_job(
            "send_email",
            email,
            "weekly_digest",
            digest_context(s, zone(tz)),
            _job_id=f"digest:{week}:{uid}",
            _queue_name=NOTIFY_QUEUE,
        )
    return len(recipients)
