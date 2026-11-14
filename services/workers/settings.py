"""ARQ worker entry points, one class per Compose service (TRD §3).

Run:    arq workers.settings.ScrapeWorker
Health: arq --check workers.settings.ScrapeWorker

Each worker refreshes `<queue>:health-check` in Redis every HEALTH_CHECK_INTERVAL
seconds; the key expires shortly after, so the API reads its presence as "worker alive".
Real jobs are added by their own tickets (HE-021 onwards).

worker-scrape has no DB credentials (Security doc §8), so this module must not import DB
code at import time: scheduler jobs import workers.scheduler inside their wrappers.
"""

import time
from typing import Any, ClassVar

from arq.connections import RedisSettings
from arq.cron import CronJob, cron

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
    functions: ClassVar[list[Any]] = [ping]
    max_jobs = 4


class NotifyWorker:
    queue_name = NOTIFY_QUEUE
    redis_settings = REDIS
    health_check_interval = HEALTH_CHECK_INTERVAL
    on_startup = install_log_redaction
    functions: ClassVar[list[Any]] = [ping, send_email]


class SchedulerWorker:
    queue_name = SCHEDULER_QUEUE
    redis_settings = REDIS
    health_check_interval = HEALTH_CHECK_INTERVAL
    on_startup = install_log_redaction
    functions: ClassVar[list[Any]] = [record_scrape]
    cron_jobs: ClassVar[list[CronJob]] = [
        cron(heartbeat, run_at_startup=True),
        cron(schedule_sources),  # every minute (second=0)
    ]
