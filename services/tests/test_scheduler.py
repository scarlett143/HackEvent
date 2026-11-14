"""HE-021: tier cadence, due-source scheduling, DB-free scrape job, result persistence.

Unit tests run everywhere; the Postgres + Redis ones need the throwaway stack (tests/stack.py).
"""

import asyncio
import hashlib
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from arq import Retry
from arq.connections import ArqRedis, RedisSettings, create_pool
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from tests.stack import DB_URL, REDIS_URL, needs_stack, sql

from common.fetch import FetchError, FetchResult, RetryLater, RobotsDisallowed
from common.fetch.ssrf import BlockedURL
from common.queues import LLM_QUEUE, SCHEDULER_QUEUE, SCRAPE_QUEUE
from workers import scheduler
from workers.scrape import job

SERVICES = Path(__file__).resolve().parents[1]


# --- cadence ---------------------------------------------------------------------------


@pytest.mark.parametrize(("tier", "hours"), [(1, 24), (2, 72), (3, 168)])
def test_cadence_per_tier(tier: int, hours: int) -> None:
    assert scheduler.cadence_hours_for("official", tier, None) == hours


def test_cadence_override_aggregator_and_missing_tier() -> None:
    assert scheduler.cadence_hours_for("official", 3, 1) == 24  # tier_override wins
    assert scheduler.cadence_hours_for("aggregator", None, None) == 4  # every few hours
    assert scheduler.cadence_hours_for("aggregator", None, 1) == 4
    assert scheduler.cadence_hours_for("reddit", None, None) == 168  # no tier → politest


# --- import boundary -------------------------------------------------------------------


def test_scrape_worker_never_imports_db_modules() -> None:
    """worker-scrape runs `arq workers.settings.ScrapeWorker` with no DATABASE_URL."""
    code = (
        "import sys, workers.settings, workers.scrape.job\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in"
        " {'sqlalchemy', 'asyncpg', 'alembic', 'psycopg', 'psycopg2'}"
        " or m in {'common.db', 'common.models', 'common.audit', 'workers.scheduler'})\n"
        "assert not bad, bad\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=SERVICES, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr


def test_enqueued_job_names_are_registered_on_the_right_worker() -> None:
    from arq.worker import Worker

    from workers import settings

    def names(cls: type) -> set[str]:
        kw = {k: v for k, v in vars(cls).items() if k in ("functions", "cron_jobs")}
        w = Worker(**kw, redis_settings=settings.REDIS)
        return set(w.functions)

    assert "scrape_source" in names(settings.ScrapeWorker)
    assert {"record_scrape", "cron:schedule_sources"} <= names(settings.SchedulerWorker)


# --- scrape job (mocked fetch, fake redis)---------------------------------------------


class _FakeRedis:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def enqueue_job(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.jobs.append((name, args, kwargs))


BODY = b"<html>Hackathon 2026</html>"
HASH = hashlib.sha256(BODY).hexdigest()
SOURCE = {
    "id": "00000000-0000-0000-0000-000000000001",
    "url": "https://example.com/events",
    "etag": '"old"',
    "last_modified": None,
    "content_hash": "previous",
    "respect_robots": True,
}


def _ok(body: bytes = BODY, *, not_modified: bool = False) -> FetchResult:
    return FetchResult(
        status=304 if not_modified else 200,
        final_url="https://example.com/events/",
        headers=httpx2.Headers({"etag": '"new"', "last-modified": "Sat, 14 Nov 2026 00:00:00 GMT"}),
        body=b"" if not_modified else body,
        content_hash=None if not_modified else hashlib.sha256(body).hexdigest(),
        not_modified=not_modified,
    )


def _mock_fetch(monkeypatch: pytest.MonkeyPatch, outcome: FetchResult | Exception) -> list[Any]:
    calls: list[Any] = []

    async def fake(url: str, etag: str | None, last_modified: str | None, **kw: Any) -> Any:
        calls.append((url, etag, last_modified, kw["respect_robots"]))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(job, "fetch", fake)
    return calls


def _handed_off(redis: _FakeRedis) -> dict[str, Any]:
    ((name, args, kwargs),) = redis.jobs
    assert name == "record_scrape" and kwargs["_queue_name"] == SCHEDULER_QUEUE
    result: dict[str, Any] = args[0]
    return result


async def test_changed_body_is_handed_off_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    big = b"a\x00" + "é".encode() * (job.BODY_MAX // 2)  # NUL + a split multibyte at 200 KB
    calls = _mock_fetch(monkeypatch, _ok(big))
    redis = _FakeRedis()
    assert await job.scrape_source({"redis": redis}, SOURCE) == "changed"
    assert calls == [(SOURCE["url"], '"old"', None, True)]
    result = _handed_off(redis)
    assert result["content_hash"] == hashlib.sha256(big).hexdigest()  # hash of the full body
    assert result["etag"] == '"new"' and result["final_url"].endswith("/events/")
    assert "\x00" not in result["body"]
    assert len(result["body"].encode()) <= job.BODY_MAX


async def test_same_hash_or_304_hands_off_no_body(monkeypatch: pytest.MonkeyPatch) -> None:
    for fetched, source in (
        (_ok(), {**SOURCE, "content_hash": HASH}),
        (_ok(not_modified=True), SOURCE),
    ):
        _mock_fetch(monkeypatch, fetched)
        redis = _FakeRedis()
        assert await job.scrape_source({"redis": redis}, source) == "unchanged"
        assert _handed_off(redis)["body"] is None


@pytest.mark.parametrize(
    "exc",
    [BlockedURL("private address"), RobotsDisallowed("robots"), FetchError("timed out")],
)
async def test_fetch_failures_are_recorded_as_errors(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    _mock_fetch(monkeypatch, exc)
    redis = _FakeRedis()
    assert await job.scrape_source({"redis": redis}, SOURCE) == "error"
    assert type(exc).__name__ in _handed_off(redis)["error"]


async def test_http_error_status_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_fetch(
        monkeypatch,
        FetchResult(404, "https://example.com/events", httpx2.Headers(), b"", "x", False),
    )
    redis = _FakeRedis()
    assert await job.scrape_source({"redis": redis}, SOURCE) == "error"
    assert _handed_off(redis)["error"] == "HTTP 404"


async def test_retry_later_defers_the_job_then_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_fetch(monkeypatch, RetryLater("rate limit", retry_after=7.5))
    redis = _FakeRedis()
    with pytest.raises(Retry) as e:
        await job.scrape_source({"redis": redis, "job_try": 1}, SOURCE)
    assert e.value.defer_score == 7500  # arq re-queues the same job id 7.5 s later
    assert redis.jobs == []
    assert await job.scrape_source({"redis": redis, "job_try": job.MAX_TRIES}, SOURCE) == "error"
    assert "retry limit" in _handed_off(redis)["error"]


# --- Postgres + Redis ------------------------------------------------------------------


@pytest.fixture
def clean(stack_client: TestClient) -> Iterator[None]:
    sql("TRUNCATE sources CASCADE")
    yield


async def q(statement: str, **params: Any) -> list[Any]:
    """tests.stack.sql from inside a running event loop."""
    return await asyncio.to_thread(sql, statement, **params)


@pytest.fixture
async def arq_redis(clean: None) -> AsyncIterator[ArqRedis]:
    assert REDIS_URL
    pool = await create_pool(RedisSettings.from_dsn(REDIS_URL))
    yield pool
    await pool.aclose()


async def _ctx(redis: ArqRedis) -> dict[str, Any]:
    assert DB_URL
    return {"redis": redis, "db": create_async_engine(DB_URL)}


async def _source(
    *, due: bool = True, enabled: bool = True, adapter: str = "llm", kind: str = "official"
) -> str:
    ((sid,),) = await q(
        "INSERT INTO sources (kind, url, adapter, cadence_hours, next_run_at, is_enabled)"
        " VALUES (CAST(:k AS source_kind), :u, CAST(:a AS source_adapter), 72,"
        " now() + :offset, :en) RETURNING id::text",
        k=kind,
        u="https://example.com/events",
        a=adapter,
        offset=timedelta(minutes=-1) if due else timedelta(hours=1),
        en=enabled,
    )
    return str(sid)


@needs_stack
async def test_due_sources_enqueued_exactly_once_across_two_schedulers(
    arq_redis: ArqRedis,
) -> None:
    due = {await _source() for _ in range(5)} | {await _source(kind="social")}
    await _source(due=False)
    await _source(enabled=False)
    a, b = await _ctx(arq_redis), await _ctx(arq_redis)
    counts = await asyncio.gather(scheduler.schedule_due(a), scheduler.schedule_due(b))
    assert sum(counts) == len(due)
    assert await scheduler.schedule_due(a) == 0  # next_run_at already advanced

    jobs = await arq_redis.queued_jobs(queue_name=SCRAPE_QUEUE)
    assert sorted(j.args[0]["id"] for j in jobs) == sorted(due)  # each due source once
    assert {j.function for j in jobs} == {"scrape_source"}
    robots = {j.args[0]["id"]: j.args[0]["respect_robots"] for j in jobs}
    assert sorted(robots.values()) == [False, True, True, True, True, True]  # social: False

    rows = await q(
        "SELECT id::text, next_run_at - now() FROM sources WHERE id = ANY(CAST(:ids AS uuid[]))",
        ids=list(due),
    )
    for _, delta in rows:  # 72 h cadence + up to 10 min jitter
        assert 72 * 3600 - 60 < delta.total_seconds() <= 72 * 3600 + 600
    for ctx in (a, b):
        await ctx["db"].dispose()


@needs_stack
async def test_same_due_slot_cannot_be_enqueued_twice(arq_redis: ArqRedis) -> None:
    """A crash after enqueue but before commit leaves next_run_at unchanged: same job id."""
    sid = await _source()
    ((slot,),) = await q("SELECT next_run_at FROM sources WHERE id = CAST(:i AS uuid)", i=sid)
    ctx = await _ctx(arq_redis)
    await scheduler.schedule_due(ctx)
    await q("UPDATE sources SET next_run_at = :t WHERE id = CAST(:i AS uuid)", t=slot, i=sid)
    await scheduler.schedule_due(ctx)
    assert len(await arq_redis.queued_jobs(queue_name=SCRAPE_QUEUE)) == 1
    await ctx["db"].dispose()


async def _scrape_and_record(
    monkeypatch: pytest.MonkeyPatch, redis: ArqRedis, sid: str, fetched: FetchResult | Exception
) -> str:
    """Run the real hand-off: scheduler payload → scrape job → record_scrape job."""
    _mock_fetch(monkeypatch, fetched)
    ctx = await _ctx(redis)
    await scheduler.schedule_due(ctx)
    (scrape,) = await redis.queued_jobs(queue_name=SCRAPE_QUEUE)
    await redis.delete(SCRAPE_QUEUE)
    await job.scrape_source({"redis": redis}, scrape.args[0])
    (record,) = await redis.queued_jobs(queue_name=SCHEDULER_QUEUE)
    await redis.delete(SCHEDULER_QUEUE)
    outcome = await scheduler.record_scrape(ctx, record.args[0])
    await ctx["db"].dispose()
    await q(
        "UPDATE sources SET next_run_at = now() - interval '1 minute' WHERE id = CAST(:i AS uuid)",
        i=sid,
    )
    return outcome


@needs_stack
async def test_changed_stores_raw_document_and_enqueues_extract(
    monkeypatch: pytest.MonkeyPatch, arq_redis: ArqRedis
) -> None:
    sid = await _source(adapter="llm")
    assert await _scrape_and_record(monkeypatch, arq_redis, sid, _ok()) == "changed"
    docs = await q("SELECT id::text, url, content_hash, body, status::text FROM raw_documents")
    assert [d[1:] for d in docs] == [("https://example.com/events/", HASH, BODY.decode(), "new")]
    (extract,) = await arq_redis.queued_jobs(queue_name=LLM_QUEUE)
    assert extract.function == "extract" and extract.args == (docs[0][0],)
    src = await q(
        "SELECT etag, last_modified, content_hash, last_success_at IS NOT NULL FROM sources"
    )
    assert src == [('"new"', "Sat, 14 Nov 2026 00:00:00 GMT", HASH, True)]

    # Same body again: hash unchanged → no new raw_document, no extract.
    await arq_redis.delete(LLM_QUEUE)
    assert await _scrape_and_record(monkeypatch, arq_redis, sid, _ok()) == "unchanged"
    assert len(await q("SELECT 1 FROM raw_documents")) == 1
    assert await arq_redis.queued_jobs(queue_name=LLM_QUEUE) == []
    # 304: validators kept, still one document.
    assert await _scrape_and_record(monkeypatch, arq_redis, sid, _ok(not_modified=True)) == (
        "unchanged"
    )
    assert await q("SELECT etag, content_hash FROM sources") == [('"new"', HASH)]


@needs_stack
async def test_non_llm_adapter_stores_document_without_extract(
    monkeypatch: pytest.MonkeyPatch, arq_redis: ArqRedis
) -> None:
    sid = await _source(adapter="rss")
    assert await _scrape_and_record(monkeypatch, arq_redis, sid, _ok()) == "changed"
    assert len(await q("SELECT 1 FROM raw_documents")) == 1
    assert await arq_redis.queued_jobs(queue_name=LLM_QUEUE) == []


@needs_stack
async def test_errors_feed_source_health(
    monkeypatch: pytest.MonkeyPatch, arq_redis: ArqRedis
) -> None:
    sid = await _source()
    for _ in range(2):
        await _scrape_and_record(monkeypatch, arq_redis, sid, BlockedURL("private address"))
    assert await q("SELECT error_streak, last_error FROM sources") == [
        (2, "BlockedURL: private address")
    ]
    assert await q("SELECT 1 FROM raw_documents") == []
    await _scrape_and_record(monkeypatch, arq_redis, sid, _ok())
    assert await q("SELECT error_streak, last_error FROM sources") == [(0, None)]
