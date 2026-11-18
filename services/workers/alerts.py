"""Ops alerts to Admin (HE-131, Runbook Part F): email + in-app notification.

`check_ops` (worker-notify cron, every minute) runs each check below. Debounce lives in
Redis: `he:alert:<check>` counts consecutive failing runs; the alert fires when it
reaches FAIL_RUNS (so a worker restart during a deploy does not page), never again while
the check keeps failing, and the key is deleted (re-armed) on the first passing run.

Telegram (HE-075) is not built, so alerts go by email (Gmail, worker-notify `send_email`)
and an in-app `notifications` row of type `ops_alert`. If worker-notify itself is down
nothing here runs: the external uptime pinger (Part F row 1) covers that.

Manual test: `docker compose exec api python -m scripts.send_test_alert`.
"""

import logging
import shutil
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from common.notifiers import Endpoint, InAppNotifier, Message
from common.queues import ALL_QUEUES, NOTIFY_QUEUE, SCHEDULER_HEARTBEAT_KEY, health_key
from workers import scheduler

log = logging.getLogger(__name__)

FAIL_RUNS = 2  # Part F: "2 failed checks"
HEARTBEAT_MAX_AGE_S = 120  # Part D: scheduler heartbeat < 2 min old
DISK_MAX_USED = 0.80  # Part F: disk > 80%
DISK_PATH = "/"  # container root = Docker's data disk on the host
REMINDER_LAG_MIN = 10  # Part F: pending reminders > 10 min past due


def disk_used_fraction(path: str = DISK_PATH) -> float:
    usage = shutil.disk_usage(path)
    return usage.used / usage.total


async def problems(conn: AsyncConnection, redis: Any) -> dict[str, tuple[str, str] | None]:
    """check name -> (problem, detail) when failing, None when passing."""
    out: dict[str, tuple[str, str] | None] = {}

    beat = await redis.get(SCHEDULER_HEARTBEAT_KEY)
    age = time.time() - int(beat) if beat is not None else None
    out["scheduler"] = (
        (
            "Scheduler stalled",
            f"Last scheduler heartbeat: {'never' if age is None else f'{int(age)} s ago'}.",
        )
        if age is None or age > HEARTBEAT_MAX_AGE_S
        else None
    )

    down = [q for q in ALL_QUEUES if not await redis.exists(health_key(q))]
    out["workers"] = ("Worker down", f"No health check from: {', '.join(down)}.") if down else None

    used = disk_used_fraction()
    out["disk"] = (
        ("Disk almost full", f"Disk {used:.0%} used (threshold {DISK_MAX_USED:.0%}).")
        if used > DISK_MAX_USED
        else None
    )

    late = (
        await conn.execute(
            text(
                "SELECT count(*) FROM reminders WHERE status = 'pending'"
                " AND scheduled_at < now() - make_interval(mins => :m)"
            ),
            {"m": REMINDER_LAG_MIN},
        )
    ).scalar_one()
    out["reminder_lag"] = (
        ("Reminders late", f"{late} pending reminder(s) more than {REMINDER_LAG_MIN} min past due.")
        if late
        else None
    )
    return out


async def alert_admins(
    conn: AsyncConnection, redis: Any, check: str, problem: str, detail: str
) -> int:
    """In-app row (this transaction) + one `send_email` job per active Admin; returns count."""
    admins = (
        await conn.execute(
            text("SELECT id, email FROM users WHERE role = 'admin' AND is_active ORDER BY email")
        )
    ).all()
    log.warning(
        "ops.alert check=%s problem=%s",
        check,
        problem,
        extra={"event": "ops.alert", "check": check},
    )
    in_app = InAppNotifier(conn)
    msg = Message(
        headline=f"Ops alert: {problem}",
        title=detail,
        when_label="",
        when="",
        path="/admin",
        kind="ops_alert",
    )
    stamp = time.time_ns()  # unique per alert; arq drops a reused job id
    for admin in admins:
        await in_app.send(Endpoint(user_id=admin.id), msg)
        await redis.enqueue_job(
            "send_email",
            admin.email,
            "ops_alert",
            {"problem": problem, "detail": detail},
            _job_id=f"ops_alert:{check}:{stamp}:{admin.email}",
            _queue_name=NOTIFY_QUEUE,
        )
    return len(admins)


async def check_ops(ctx: dict[str, Any]) -> list[str]:
    """Run every check; alert on the FAIL_RUNS-th consecutive failure. Returns alerted checks."""
    redis = ctx["redis"]
    alerted: list[str] = []
    async with scheduler.db_engine(ctx).begin() as conn:
        for check, found in (await problems(conn, redis)).items():
            key = f"he:alert:{check}"
            if found is None:
                await redis.delete(key)
            elif await redis.incr(key) == FAIL_RUNS:
                try:
                    await alert_admins(conn, redis, check, *found)
                except Exception:
                    await redis.decr(key)  # not alerted: the next run tries again
                    raise
                alerted.append(check)
    return alerted
