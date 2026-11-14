"""HE-046: content-hash cache (real Postgres + Redis). Acceptance: repeat page = 0 LLM calls.

Providers are faked at `call_llm` (caps/routing: test_llm_router.py); the provider row is
real because llm_cache.provider_id is a foreign key. All data is synthetic.
"""

import uuid
from datetime import timedelta
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.stack import needs_stack
from tests.test_extraction import NOW, PAGE, SRC, event
from tests.test_llm_router import add_provider, engine, rds, stack  # noqa: F401 (fixtures)
from tests.test_verify import ALL_OK

from common.llm import LLMResult, NoProviderAvailable
from common.models import LlmCache, LlmProviderRole
from workers.extraction import ExtractionResult, extract, pipeline
from workers.extraction import verify as verify_mod

pytestmark = needs_stack


class Fake:
    def __init__(self, pid: uuid.UUID, confidence: float = 0.9, verify: Any = None) -> None:
        self.pid, self.confidence, self.verify = pid, confidence, verify
        self.roles: list[LlmProviderRole] = []

    async def __call__(self, _engine: Any, _redis: Any, **kw: Any) -> LLMResult:
        self.roles.append(kw["role"])
        if kw["role"] == LlmProviderRole.extract:
            data = {"is_hackathon": True, "events": [event(confidence=self.confidence)]}
        elif isinstance(self.verify, Exception):
            raise self.verify
        else:
            data = self.verify
        return LLMResult(data, 10, 10, "m", "p", 5, provider_id=self.pid)


async def cached(engine: AsyncEngine) -> int:  # noqa: F811
    async with AsyncSession(engine) as db:
        return int(await db.scalar(select(func.count()).select_from(LlmCache)) or 0)


@pytest.fixture
async def pid(engine: AsyncEngine) -> uuid.UUID:  # noqa: F811
    return await add_provider(engine, "ex")


async def go(
    engine: AsyncEngine,  # noqa: F811
    rds: Redis,  # noqa: F811
    fake: Fake,
    monkeypatch: pytest.MonkeyPatch,
    **kw: Any,
) -> ExtractionResult:
    monkeypatch.setattr(pipeline, "call_llm", fake)
    monkeypatch.setattr(verify_mod, "call_llm", fake)
    args: dict[str, Any] = {"html": PAGE, "source_url": SRC, "fetched_at": NOW, "now": NOW}
    return await extract(engine, rds, default_tz="Asia/Kolkata", **(args | kw))


async def test_repeat_page_makes_zero_llm_calls(
    engine: AsyncEngine,  # noqa: F811
    rds: Redis,  # noqa: F811
    pid: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = Fake(pid)
    first = await go(engine, rds, fake, monkeypatch)
    # same page text fetched again later: still a hit
    second = await go(engine, rds, fake, monkeypatch, fetched_at=NOW + timedelta(days=1))
    assert fake.roles == [LlmProviderRole.extract]
    assert second == first and first.events[0].extractor == "p:m"
    assert await cached(engine) == 1


async def test_cache_hit_recomputes_status(
    engine: AsyncEngine,  # noqa: F811
    rds: Redis,  # noqa: F811
    pid: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = Fake(pid)
    assert (await go(engine, rds, fake, monkeypatch)).events[0].status == "open"
    later = await go(engine, rds, fake, monkeypatch, now=NOW + timedelta(days=200))
    assert later.events[0].status == "closed" and len(fake.roles) == 1


async def test_schema_version_bump_misses(
    engine: AsyncEngine,  # noqa: F811
    rds: Redis,  # noqa: F811
    pid: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = Fake(pid)
    await go(engine, rds, fake, monkeypatch)
    monkeypatch.setattr(pipeline, "SCHEMA_VERSION", 2)
    await go(engine, rds, fake, monkeypatch)
    assert len(fake.roles) == 2


async def test_verified_run_is_cached_with_its_reasons(
    engine: AsyncEngine,  # noqa: F811
    rds: Redis,  # noqa: F811
    pid: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    v = {"confidence": 0.9, "fields": ALL_OK | {"registration_deadline": {"supported": False}}}
    fake = Fake(pid, confidence=0.5, verify=v)
    first = await go(engine, rds, fake, monkeypatch)
    second = await go(engine, rds, fake, monkeypatch)
    assert fake.roles == [LlmProviderRole.extract, LlmProviderRole.verify]
    assert second.events[0].review_reasons == first.events[0].review_reasons
    assert "deadline_unsupported" in second.events[0].review_reasons


async def test_unverified_run_is_not_cached(
    engine: AsyncEngine,  # noqa: F811
    rds: Redis,  # noqa: F811
    pid: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = Fake(pid, confidence=0.5, verify=NoProviderAvailable("none"))
    await go(engine, rds, fake, monkeypatch)
    await go(engine, rds, fake, monkeypatch)
    assert fake.roles.count(LlmProviderRole.extract) == 2
    assert await cached(engine) == 0
