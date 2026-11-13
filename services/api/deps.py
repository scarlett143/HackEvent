"""Per-request DB session and Redis client, built on the lifespan-owned `Deps`."""

from collections.abc import AsyncIterator

from fastapi import Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from api.health import Deps


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    deps: Deps = request.app.state.deps
    async with AsyncSession(deps.engine, expire_on_commit=False) as db:
        yield db


def get_redis(request: Request) -> Redis:
    deps: Deps = request.app.state.deps
    return deps.redis
