"""ARQ worker entry points, one class per Compose service (TRD §3).

Run:    arq workers.settings.ScrapeWorker
Health: arq --check workers.settings.ScrapeWorker

Each worker refreshes `<queue>:health-check` in Redis every HEALTH_CHECK_INTERVAL
seconds; the key expires shortly after, so the API reads its presence as "worker alive".
Real jobs are added by their own tickets (HE-021 onwards).
"""

import time
from typing import Any, ClassVar

from arq.connections import RedisSettings
from arq.cron import CronJob, cron

from common.config import get_settings
from common.queues import (
    LLM_QUEUE,
    NOTIFY_QUEUE,
    SCHEDULER_HEARTBEAT_KEY,
    SCHEDULER_QUEUE,
    SCRAPE_QUEUE,
)

HEALTH_CHECK_INTERVAL = 30


async def ping(ctx: dict[str, Any]) -> str:
    return "pong"


async def heartbeat(ctx: dict[str, Any]) -> None:
    """Runbook Part D: scheduler heartbeat must be < 2 min old."""
    await ctx["redis"].set(SCHEDULER_HEARTBEAT_KEY, str(int(time.time())), ex=600)


# arq reads only each class's own __dict__ (arq.worker.get_kwargs), so settings are
# repeated per class on purpose; inherited attributes would be silently ignored.
REDIS = RedisSettings.from_dsn(get_settings().redis_url)


class ScrapeWorker:
    queue_name = SCRAPE_QUEUE
    redis_settings = REDIS
    health_check_interval = HEALTH_CHECK_INTERVAL
    functions: ClassVar[list[Any]] = [ping]
    max_jobs = 2


class LlmWorker:
    queue_name = LLM_QUEUE
    redis_settings = REDIS
    health_check_interval = HEALTH_CHECK_INTERVAL
    functions: ClassVar[list[Any]] = [ping]
    max_jobs = 4


class NotifyWorker:
    queue_name = NOTIFY_QUEUE
    redis_settings = REDIS
    health_check_interval = HEALTH_CHECK_INTERVAL
    functions: ClassVar[list[Any]] = [ping]


class SchedulerWorker:
    queue_name = SCHEDULER_QUEUE
    redis_settings = REDIS
    health_check_interval = HEALTH_CHECK_INTERVAL
    cron_jobs: ClassVar[list[CronJob]] = [cron(heartbeat, run_at_startup=True)]
