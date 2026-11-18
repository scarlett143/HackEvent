"""ARQ worker entry points, one class per Compose service (TRD §3).

Run:    arq workers.settings.ScrapeWorker
Health: arq --check workers.settings.ScrapeWorker

Each worker refreshes `<queue>:health-check` in Redis every HEALTH_CHECK_INTERVAL
seconds; the key expires shortly after, so the API reads its presence as "worker alive".

worker-scrape has no DB credentials (Security doc §8), so this module must not import DB
code at import time: scheduler jobs import workers.scheduler inside their wrappers.
"""

import time
from typing import Any, ClassVar

from arq.connections import RedisSettings
from arq.cron import CronJob, cron
from arq.worker import func

from common.config import get_settings
from common.email import GmailSender, render
from common.logging import configure_logging
from common.queues import (
    LLM_QUEUE,
    NOTIFY_QUEUE,
    SCHEDULER_HEARTBEAT_KEY,
    SCHEDULER_QUEUE,
    SCRAPE_QUEUE,
)
from workers.scrape.job import MAX_TRIES, scrape_source

HEALTH_CHECK_INTERVAL = 30


async def ping(ctx: dict[str, Any]) -> str:
    return "pong"


async def heartbeat(ctx: dict[str, Any]) -> None:
    """Runbook Part D: scheduler heartbeat must be < 2 min old."""
    await ctx["redis"].set(SCHEDULER_HEARTBEAT_KEY, str(int(time.time())), ex=600)


async def send_email(
    ctx: dict[str, Any], to: str, template: str, context: dict[str, Any] | None = None
) -> str:
    """Render `template` and send it via Gmail; returns the Gmail message id.

    The sender lives in the worker ctx so its access-token cache survives across jobs.
    TokenRevoked / EmailError fail the job (no arq retry: it needs a human or is logged).
    """
    if "gmail" not in ctx:
        ctx["gmail"] = GmailSender.from_settings()
    sender: GmailSender = ctx["gmail"]
    return await sender.send(to, render(template, **(context or {})))


async def schedule_sources(ctx: dict[str, Any]) -> int:
    from workers.scheduler import schedule_due

    return await schedule_due(ctx)


async def record_scrape(ctx: dict[str, Any], result: dict[str, Any]) -> str:
    from workers import scheduler

    return await scheduler.record_scrape(ctx, result)


async def record_candidates(ctx: dict[str, Any], source_id: str, count: int) -> str:
    from workers import scheduler

    return await scheduler.record_candidates(ctx, source_id, count)


async def ingest_document(ctx: dict[str, Any], raw_document_id: str) -> str:
    from workers import ingest

    return await ingest.ingest_document(ctx, raw_document_id)


async def dispatch_reminders(ctx: dict[str, Any]) -> int:
    from workers import reminders

    return await reminders.dispatch_due(ctx)


async def send_reminder(ctx: dict[str, Any], reminder_id: str) -> str:
    from workers import reminders

    return await reminders.send_reminder(ctx, reminder_id)


async def check_ops(ctx: dict[str, Any]) -> list[str]:
    from workers import alerts

    return await alerts.check_ops(ctx)


async def install_log_redaction(ctx: dict[str, Any]) -> None:
    """arq on_startup: its CLI runs dictConfig after importing this module."""
    configure_logging()


# arq reads only each class's own __dict__ (arq.worker.get_kwargs), so settings are
# repeated per class on purpose; inherited attributes would be silently ignored.
REDIS = RedisSettings.from_dsn(get_settings().redis_url)


class ScrapeWorker:
    queue_name = SCRAPE_QUEUE
    redis_settings = REDIS
    health_check_interval = HEALTH_CHECK_INTERVAL
    on_startup = install_log_redaction
    functions: ClassVar[list[Any]] = [ping, scrape_source]
    max_jobs = 2
    max_tries = MAX_TRIES


class LlmWorker:
    queue_name = LLM_QUEUE
    redis_settings = REDIS
    health_check_interval = HEALTH_CHECK_INTERVAL
    on_startup = install_log_redaction
    # LLM_MAX_TRIES in workers.ingest; `extract` = ingest_document for adapter=llm pages.
    functions: ClassVar[list[Any]] = [ping, func(ingest_document, name="extract")]
    max_jobs = 4
    max_tries = 5


class NotifyWorker:
    queue_name = NOTIFY_QUEUE
    redis_settings = REDIS
    health_check_interval = HEALTH_CHECK_INTERVAL
    on_startup = install_log_redaction
    functions: ClassVar[list[Any]] = [ping, send_email, send_reminder]
    # HE-131: here, not on the scheduler, so a stalled scheduler is still reported.
    cron_jobs: ClassVar[list[CronJob]] = [cron(check_ops, second=45)]


class SchedulerWorker:
    queue_name = SCHEDULER_QUEUE
    redis_settings = REDIS
    health_check_interval = HEALTH_CHECK_INTERVAL
    on_startup = install_log_redaction
    functions: ClassVar[list[Any]] = [record_scrape, record_candidates, ingest_document]
    cron_jobs: ClassVar[list[CronJob]] = [
        cron(heartbeat, run_at_startup=True),
        cron(schedule_sources),  # every minute (second=0)
        cron(dispatch_reminders, second=30),  # HE-071: every minute, offset from sources
    ]
