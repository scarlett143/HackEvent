"""Manual acceptance test for HE-131: send a test ops alert to every active Admin.

    docker compose exec api python -m scripts.send_test_alert

Same path as a real alert (workers.alerts.alert_admins): an in-app notification now, and a
`send_email` job that worker-notify delivers. Then check each Admin's Inbox and in-app inbox.
"""

import asyncio

from arq.connections import ArqRedis, RedisSettings, create_pool
from sqlalchemy.ext.asyncio import create_async_engine

from common.config import get_settings
from common.logging import configure_logging
from workers.alerts import alert_admins


async def _send() -> int:
    s = get_settings()
    engine = create_async_engine(s.database_url)
    redis: ArqRedis = await create_pool(RedisSettings.from_dsn(s.redis_url))
    try:
        async with engine.begin() as conn:
            return await alert_admins(
                conn, redis, "test", "Test alert", "This is a test alert. No action needed."
            )
    finally:
        await redis.aclose()
        await engine.dispose()


def main() -> None:
    configure_logging()
    n = asyncio.run(_send())
    if n == 0:
        raise SystemExit("FAILED: no active Admin to alert.")
    print(f"Queued for {n} Admin(s). Check their Inbox (not Spam) and the in-app inbox.")


if __name__ == "__main__":
    main()
