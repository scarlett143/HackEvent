"""Dependency checks behind GET /api/v1/health (Runbook Part D)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from common.config import get_settings
from common.queues import ALL_QUEUES, health_key


@dataclass
class Deps:
    engine: AsyncEngine
    redis: Redis


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s = get_settings()
    deps = Deps(
        engine=create_async_engine(s.database_url, pool_pre_ping=True, pool_size=2),
        redis=Redis.from_url(s.redis_url, socket_timeout=2),
    )
    app.state.deps = deps
    try:
        yield
    finally:
        await deps.redis.aclose()
        await deps.engine.dispose()


async def check_db(deps: Deps) -> bool:
    try:
        async with deps.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except (SQLAlchemyError, OSError):
        return False


async def check_redis(deps: Deps) -> bool:
    try:
        return bool(await deps.redis.ping())
    except (RedisError, OSError):
        return False


async def check_workers(deps: Deps) -> bool:
    try:
        return int(await deps.redis.exists(*(health_key(q) for q in ALL_QUEUES))) == len(ALL_QUEUES)
    except (RedisError, OSError):
        return False
