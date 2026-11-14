"""HE-043: call_llm routing, cost caps (fail-closed, atomic reservation), fallback, usage.

Real Postgres + Redis (skipped unless TEST_DATABASE_URL / TEST_REDIS_URL, see stack.py);
provider HTTP is a MockTransport keyed by model name, so no network and synthetic keys.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx2
import pytest
import pytest_asyncio
import redis
from alembic import command
from alembic.config import Config
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from tests.stack import ALEMBIC_INI, DB_URL, REDIS_URL, needs_stack, sql
from tests.test_llm import GOOD, KEY, MASTER, SCHEMA, _gemini_ok

from common import crypto
from common.llm import NoProviderAvailable, call_llm
from common.llm import router as llm_router
from common.llm.router import cooldown_key, cost_usd, estimate_input_tokens
from common.models import LlmProvider, LlmProviderRole, LlmProviderType, LlmUsage

pytestmark = needs_stack

SYSTEM, USER = "sys prompt SECRET-PROMPT", "page text SECRET-PAGE"
MAX_OUT = 1000
Resp = tuple[int, Any, dict[str, str]]
OK: Resp = (200, _gemini_ok(json.dumps(GOOD), 1200, 40), {})
BAD_JSON: Resp = (200, _gemini_ok("not json", 1200, 40), {})


class Http:
    """Mock provider HTTP: per-model queued responses (last repeats), default OK."""

    def __init__(self) -> None:
        self.responses: dict[str, list[Resp]] = {}
        self.calls: list[str] = []
        self.gate: asyncio.Event | None = None

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
        model = request.url.path.split("/models/")[1].split(":")[0]
        self.calls.append(model)
        if self.gate:
            await self.gate.wait()
        queue = self.responses.get(model) or [OK]
        status, body, headers = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx2.Response(status, json=body, headers=headers)


@pytest.fixture
def stack() -> Iterator[None]:
    assert DB_URL and REDIS_URL
    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["skip_logging"] = True
    cfg.attributes["database_url"] = DB_URL
    command.upgrade(cfg, "head")
    sql("TRUNCATE llm_providers CASCADE")
    redis.Redis.from_url(REDIS_URL).flushdb()
    yield


@pytest_asyncio.fixture
async def engine(stack: None) -> AsyncIterator[AsyncEngine]:
    assert DB_URL
    eng = create_async_engine(DB_URL, pool_size=20)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def rds(stack: None) -> AsyncIterator[Redis]:
    assert REDIS_URL
    r = Redis.from_url(REDIS_URL)
    yield r
    await r.aclose()


@pytest.fixture
def http() -> Http:
    return Http()


async def add_provider(
    engine: AsyncEngine,
    name: str,
    *,
    role: LlmProviderRole = LlmProviderRole.extract,
    priority: int = 0,
    price: str = "1",
    daily: str = "100",
    monthly: str = "1000",
    enabled: bool = True,
) -> uuid.UUID:
    pid = uuid.uuid4()
    enc = crypto.encrypt(KEY.encode(), crypto.aad("llm_providers", "api_key_enc", pid), MASTER)
    async with AsyncSession(engine) as db, db.begin():
        db.add(
            LlmProvider(
                id=pid,
                name=name,
                type=LlmProviderType.gemini,
                base_url=None,
                model=name,  # Http routes by model
                api_key_enc=enc,
                role=role,
                priority=priority,
                input_price_per_mtok=Decimal(price),
                output_price_per_mtok=Decimal(price),
                daily_cap_usd=Decimal(daily),
                monthly_cap_usd=Decimal(monthly),
                is_enabled=enabled,
            )
        )
    return pid


async def add_usage(engine: AsyncEngine, pid: uuid.UUID, cost: str, at: datetime) -> None:
    async with AsyncSession(engine) as db, db.begin():
        db.add(
            LlmUsage(
                provider_id=pid,
                job_id="old",
                input_tokens=0,
                output_tokens=0,
                cost_usd=Decimal(cost),
                cache_hit=False,
                ok=True,
                at=at,
                status="ok",
            )
        )


async def usage(engine: AsyncEngine) -> list[LlmUsage]:
    async with AsyncSession(engine) as db:
        return list((await db.scalars(select(LlmUsage).order_by(LlmUsage.at))).all())


async def run(
    engine: AsyncEngine, rds: Redis, http: Http, role: LlmProviderRole = LlmProviderRole.extract
) -> Any:
    return await call_llm(
        engine,
        rds,
        role=role,
        system=SYSTEM,
        user=USER,
        schema=SCHEMA,
        max_output_tokens=MAX_OUT,
        purpose="extract",
        job_id="job-1",
        client=httpx2.AsyncClient(transport=httpx2.MockTransport(http)),
        master_key=MASTER,
    )


def estimate(price: str = "1") -> Decimal:
    p = Decimal(price)
    return cost_usd(estimate_input_tokens(SYSTEM, USER, SCHEMA), MAX_OUT, p, p)


async def test_over_cap_uses_fallback(
    engine: AsyncEngine, rds: Redis, http: Http, caplog: pytest.LogCaptureFixture
) -> None:
    """Acceptance: primary over its daily cap -> no call to it, fallback answers."""
    primary = await add_provider(engine, "primary", daily="1")
    await add_provider(engine, "backup", role=LlmProviderRole.fallback)
    await add_usage(engine, primary, "1", datetime.now(UTC))
    result = await run(engine, rds, http)
    assert result.data == GOOD
    assert http.calls == ["backup"]
    assert "llm.cap_hit" in caplog.text and "window=day" in caplog.text


async def test_monthly_cap_also_enforced(engine: AsyncEngine, rds: Redis, http: Http) -> None:
    primary = await add_provider(engine, "primary", daily="100", monthly="2")
    await add_provider(engine, "backup", role=LlmProviderRole.fallback)
    await add_usage(engine, primary, "2", datetime.now(UTC))
    await run(engine, rds, http)
    assert http.calls == ["backup"]


async def test_all_over_cap_no_http(
    engine: AsyncEngine, rds: Redis, http: Http, caplog: pytest.LogCaptureFixture
) -> None:
    a = await add_provider(engine, "a", daily="1")
    b = await add_provider(engine, "b", role=LlmProviderRole.fallback, monthly="1")
    await add_usage(engine, a, "1", datetime.now(UTC))
    await add_usage(engine, b, "1", datetime.now(UTC))
    with pytest.raises(NoProviderAvailable):
        await run(engine, rds, http)
    assert http.calls == []
    assert len(await usage(engine)) == 2  # no reservation rows left behind
    assert "llm.no_provider_available" in caplog.text


async def test_estimate_alone_over_cap_fails_closed(
    engine: AsyncEngine, rds: Redis, http: Http
) -> None:
    await add_provider(engine, "pricey", price="100000", daily="1")  # est >> $1
    with pytest.raises(NoProviderAvailable):
        await run(engine, rds, http)
    assert http.calls == []


async def test_unknown_price_fails_closed(
    engine: AsyncEngine, rds: Redis, http: Http, caplog: pytest.LogCaptureFixture
) -> None:
    await add_provider(engine, "free", price="0")
    with pytest.raises(NoProviderAvailable):
        await run(engine, rds, http)
    assert http.calls == []
    assert "llm.price_unknown" in caplog.text


async def test_routing_order_role_priority_disabled(
    engine: AsyncEngine, rds: Redis, http: Http
) -> None:
    await add_provider(engine, "ex2", priority=2)
    await add_provider(engine, "ex1", priority=1)
    await add_provider(engine, "off", priority=0, enabled=False)
    await add_provider(engine, "fb0", role=LlmProviderRole.fallback, priority=0)
    await add_provider(engine, "ver0", role=LlmProviderRole.verify, priority=0)
    bad: list[Resp] = [(400, {"error": {"status": "INVALID_ARGUMENT"}}, {})]
    http.responses = {"ex1": list(bad), "ex2": list(bad), "fb0": list(bad)}
    with pytest.raises(NoProviderAvailable):
        await run(engine, rds, http)
    assert http.calls == ["ex1", "ex2", "fb0"]
    assert {u.status for u in await usage(engine)} == {"error"}
    http.calls.clear()
    await run(engine, rds, http, role=LlmProviderRole.verify)
    assert http.calls == ["ver0"]


async def test_settle_records_actual_usage(engine: AsyncEngine, rds: Redis, http: Http) -> None:
    pid = await add_provider(engine, "p", price="2")
    await run(engine, rds, http)
    (row,) = await usage(engine)
    assert row.provider_id == pid
    assert (row.status, row.ok, row.input_tokens, row.output_tokens) == ("ok", True, 1200, 40)
    assert row.cost_usd == cost_usd(1200, 40, Decimal(2), Decimal(2)) == Decimal("0.00248")
    assert row.cost_usd < estimate("2")
    assert (row.purpose, row.job_id, row.model) == ("extract", "job-1", "gemini-3.5-flash-lite")
    assert row.latency_ms is not None and row.latency_ms >= 0


async def test_rate_limited_cools_down_and_falls_back(
    engine: AsyncEngine, rds: Redis, http: Http
) -> None:
    primary = await add_provider(engine, "primary")
    await add_provider(engine, "backup", role=LlmProviderRole.fallback)
    http.responses["primary"] = [(429, {}, {"retry-after": "30"})]
    await run(engine, rds, http)
    assert http.calls == ["primary", "backup"]
    assert 25 <= await rds.ttl(cooldown_key(primary)) <= 30
    limited = [u for u in await usage(engine) if u.status == "rate_limited"]
    assert len(limited) == 1 and limited[0].cost_usd == 0
    http.calls.clear()
    await run(engine, rds, http)  # still cooling down: primary not even tried
    assert http.calls == ["backup"]


async def test_invalid_output_retries_once_then_falls_back(
    engine: AsyncEngine, rds: Redis, http: Http
) -> None:
    await add_provider(engine, "primary")
    await add_provider(engine, "backup", role=LlmProviderRole.fallback)
    http.responses["primary"] = [BAD_JSON]
    result = await run(engine, rds, http)
    assert result.data == GOOD
    assert http.calls == ["primary", "primary", "backup"]
    rows = await usage(engine)
    assert sorted(u.status or "" for u in rows) == ["invalid_output", "invalid_output", "ok"]
    # tokens unknown on failure -> the reservation (estimate) is kept: fail closed
    assert all(u.cost_usd == estimate() for u in rows if u.status == "invalid_output")


async def test_usage_and_logs_hold_no_prompt_or_key(
    engine: AsyncEngine, rds: Redis, http: Http, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    await add_provider(engine, "primary")
    await add_provider(engine, "backup", role=LlmProviderRole.fallback)
    http.responses["primary"] = [(429, {}, {}), OK]
    await run(engine, rds, http)
    async with engine.connect() as conn:
        dump = str((await conn.execute(text("SELECT * FROM llm_usage"))).all())
    for secret in ("SECRET-PROMPT", "SECRET-PAGE", KEY):
        assert secret not in dump
        assert secret not in caplog.text


async def test_reservation_is_atomic_under_concurrency(
    engine: AsyncEngine, rds: Redis, http: Http
) -> None:
    """20 parallel calls, cap fits 4 estimates: exactly 4 reach HTTP, in-flight sum <= cap."""
    price = "100"
    est = estimate(price)
    cap = (est * 4 + est / 2).quantize(Decimal("0.01"))  # room for 4, not 5
    await add_provider(engine, "p", price=price, daily=str(cap))
    http.gate = asyncio.Event()
    tasks = [asyncio.create_task(run(engine, rds, http)) for _ in range(20)]
    async with asyncio.timeout(10):
        while len(http.calls) < 4 or sum(t.done() for t in tasks) < 16:
            await asyncio.sleep(0.01)
    rows = await usage(engine)
    assert len(rows) == 4 and all(u.status == "reserved" for u in rows)
    assert sum(u.cost_usd for u in rows) <= cap
    http.gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert sum(isinstance(r, NoProviderAvailable) for r in results) == 16
    assert len(http.calls) == 4
    assert all(u.status == "ok" for u in await usage(engine))


async def test_day_and_month_rollover_utc(
    engine: AsyncEngine, rds: Redis, http: Http, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = await add_provider(engine, "p", daily="1", monthly="1.5")
    await add_usage(engine, pid, "1", datetime(2026, 11, 14, 23, 59, 59, tzinfo=UTC))
    now = datetime(2026, 11, 15, 0, 0, 5, tzinfo=UTC)
    monkeypatch.setattr(llm_router, "_utcnow", lambda: now)
    await run(engine, rds, http)  # new UTC day: daily window empty, month 1.00 of 1.50
    await add_usage(engine, pid, "0.6", datetime(2026, 11, 15, 0, 0, 1, tzinfo=UTC))
    with pytest.raises(NoProviderAvailable):  # month now over 1.50
        await run(engine, rds, http)
    now = datetime(2026, 12, 1, 0, 0, 5, tzinfo=UTC)
    await run(engine, rds, http)  # new UTC month: both windows empty
    assert http.calls == ["p", "p"]
