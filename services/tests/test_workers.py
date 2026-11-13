from typing import Any

import pytest
from arq.worker import get_kwargs

from common.queues import ALL_QUEUES, SCHEDULER_HEARTBEAT_KEY
from workers import settings


def _kw(cls: Any) -> dict[str, Any]:
    # arq annotates get_kwargs' return type incorrectly; normalise for typing
    return dict(get_kwargs(cls))


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, tuple[str, int | None]] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = (value, ex)


async def test_heartbeat_writes_expiring_timestamp() -> None:
    redis = _FakeRedis()
    ctx: dict[str, Any] = {"redis": redis}
    await settings.heartbeat(ctx)
    value, ex = redis.store[SCHEDULER_HEARTBEAT_KEY]
    assert value.isdigit()
    assert ex is not None and ex > 120


def test_each_worker_has_its_own_queue_and_fast_health_check() -> None:
    classes = [
        settings.ScrapeWorker,
        settings.LlmWorker,
        settings.NotifyWorker,
        settings.SchedulerWorker,
    ]
    kwargs = [_kw(c) for c in classes]
    assert sorted(k["queue_name"] for k in kwargs) == sorted(ALL_QUEUES)
    for k in kwargs:
        # health key must refresh well inside the Compose healthcheck window
        assert k["health_check_interval"] <= 30


def test_concurrency_matches_trd() -> None:
    assert settings.ScrapeWorker.max_jobs == 2
    assert settings.LlmWorker.max_jobs == 4


@pytest.mark.parametrize(
    "cls",
    [settings.ScrapeWorker, settings.LlmWorker, settings.NotifyWorker, settings.SchedulerWorker],
)
def test_arq_sees_every_setting(cls: type) -> None:
    """arq ignores inherited attributes; check through its own reader."""
    kwargs = _kw(cls)
    assert {"queue_name", "redis_settings", "health_check_interval"} <= kwargs.keys()
    assert kwargs.get("functions") or kwargs.get("cron_jobs")
