"""workers.ingest: stored raw_documents -> adapter / LLM extract -> upsert_candidate."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from arq import Retry
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from tests.stack import DB_URL, needs_stack, sql

from common.llm import NoProviderAvailable
from workers import ingest
from workers.extraction import ExtractedCandidate, ExtractionResult
from workers.ingest import parser_for

DEVPOST = Path(__file__).parent / "fixtures" / "adapters" / "devpost"
DEVPOST_BODY = (DEVPOST / "hackathons_open_upcoming_page3.json").read_text()
DEVPOST_URL = "https://devpost.com/api/hackathons?status[]=upcoming&status[]=open"


def test_parser_for() -> None:
    assert parser_for("rss", None) == "rss"
    assert parser_for("api", {"parser": "devpost"}) == "devpost"
    assert parser_for("selector", None) == "selector"  # no parser -> UnknownAdapter


# --- Postgres ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self) -> None:
        self.jobs: list[str] = []

    async def enqueue_job(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.jobs.append(name)


@pytest.fixture
def clean(stack_client: TestClient) -> Iterator[None]:
    sql("TRUNCATE organisations, sources, events CASCADE")
    yield


@pytest.fixture
async def ctx(clean: None) -> AsyncIterator[dict[str, Any]]:
    assert DB_URL
    engine = create_async_engine(DB_URL)
    yield {"redis": _FakeRedis(), "db": engine, "job_try": 1, "job_id": "t"}
    await engine.dispose()


async def q(statement: str, **params: Any) -> list[Any]:
    return await asyncio.to_thread(sql, statement, **params)


async def _doc(adapter: str, body: str, config: str | None = None) -> tuple[str, str]:
    ((sid,),) = await q(
        "INSERT INTO sources (kind, url, adapter, selector_config, cadence_hours, is_enabled,"
        " content_hash) VALUES ('aggregator', :u, CAST(:a AS source_adapter),"
        " CAST(:c AS jsonb), 4, true, 'h') RETURNING id::text",
        u=DEVPOST_URL,
        a=adapter,
        c=config,
    )
    ((rid,),) = await q(
        "INSERT INTO raw_documents (source_id, url, content_hash, body, status)"
        " VALUES (CAST(:s AS uuid), :u, 'h', :b, 'new') RETURNING id::text",
        s=sid,
        u=DEVPOST_URL,
        b=body,
    )
    return str(sid), str(rid)


async def _state(rid: str) -> tuple[Any, ...]:
    ((doc_status, errors, empties, chash),) = await q(
        "SELECT d.status::text, s.error_streak, s.empty_streak, s.content_hash"
        " FROM raw_documents d JOIN sources s ON s.id = d.source_id WHERE d.id = CAST(:r AS uuid)",
        r=rid,
    )
    return doc_status, errors, empties, chash


@needs_stack
async def test_api_document_upserts_events_once(ctx: dict[str, Any]) -> None:
    _, rid = await _doc("api", DEVPOST_BODY, '{"parser": "devpost"}')
    assert await ingest.ingest_document(ctx, rid) == "parsed"
    n_events = len(await q("SELECT 1 FROM events"))
    assert n_events > 0
    links = await q(
        "SELECT count(*) FROM event_sources WHERE raw_document_id = CAST(:r AS uuid)", r=rid
    )
    assert links == [(n_events,)]
    assert await _state(rid) == ("parsed", 0, 0, "h")
    assert await ingest.ingest_document(ctx, rid) == "skipped"  # re-delivered job: no-op
    assert len(await q("SELECT 1 FROM events")) == n_events


@needs_stack
async def test_parse_error_fails_document_and_counts_as_source_error(
    ctx: dict[str, Any],
) -> None:
    _, rid = await _doc("rss", "<rss><channel><item>")
    assert await ingest.ingest_document(ctx, rid) == "failed"
    assert await _state(rid) == ("failed", 1, 0, "h")
    ((err,),) = await q("SELECT last_error FROM sources")
    assert err.startswith("ParseError")
    _, rid = await _doc("selector", "<html/>")  # selector without a parser
    assert await ingest.ingest_document(ctx, rid) == "failed"
    assert await _state(rid) == ("failed", 1, 0, "h")
    assert await q("SELECT 1 FROM events") == []


@needs_stack
async def test_llm_document_goes_through_extract(
    ctx: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    async def fake_extract(engine: Any, redis: Any, **kw: Any) -> ExtractionResult:
        seen.update(kw)
        if "Hackathon" not in kw["html"]:  # what the keyword pre-filter would return
            return ExtractionResult(is_hackathon=False, events=[])
        event = ExtractedCandidate(
            title="Smart Campus Hackathon",
            organiser="Example Institute",
            registration_deadline=datetime(2027, 1, 10, tzinfo=UTC),
            confidence=0.95,
            extractor="gemini:test",
        )
        return ExtractionResult(is_hackathon=True, events=[event])

    monkeypatch.setattr(ingest, "extract", fake_extract)
    _, rid = await _doc("llm", "<html>Smart Campus Hackathon</html>")
    assert await ingest.ingest_document(ctx, rid) == "parsed"
    assert seen["source_url"] == DEVPOST_URL and seen["default_tz"] is None
    assert await q("SELECT title, organiser_name FROM events") == [
        ("Smart Campus Hackathon", "Example Institute")
    ]
    assert await q("SELECT extractor FROM event_sources") == [("gemini:test",)]

    _, rid = await _doc("llm", "<html>careers page</html>")
    assert await ingest.ingest_document(ctx, rid) == "parsed"
    assert await _state(rid) == ("parsed", 0, 1, "h")  # 0 candidates -> empty run


@needs_stack
async def test_india_organiser_pages_default_to_ist(
    ctx: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    async def fake_extract(engine: Any, redis: Any, **kw: Any) -> ExtractionResult:
        seen.update(kw)
        return ExtractionResult(is_hackathon=False, events=[])

    monkeypatch.setattr(ingest, "extract", fake_extract)
    sid, rid = await _doc("llm", "<html/>")
    ((oid,),) = await q(
        "INSERT INTO organisations (name, kind, region, tier, rank_source, website, is_active)"
        " SELECT 'IIT X', kind, 'india', 1, 'nirf', 'https://iitx.example', true"
        " FROM (SELECT unnest(enum_range(NULL::org_kind)) AS kind LIMIT 1) k RETURNING id::text"
    )
    await q(
        "UPDATE sources SET organisation_id = CAST(:o AS uuid) WHERE id = CAST(:s AS uuid)",
        o=oid,
        s=sid,
    )
    assert await ingest.ingest_document(ctx, rid) == "parsed"
    assert seen["default_tz"] == "Asia/Kolkata"


@needs_stack
async def test_no_llm_provider_retries_then_forces_refetch(
    ctx: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unavailable(engine: Any, redis: Any, **kw: Any) -> ExtractionResult:
        raise NoProviderAvailable("all over cap")

    monkeypatch.setattr(ingest, "extract", unavailable)
    _, rid = await _doc("llm", "<html>Hackathon</html>")
    with pytest.raises(Retry):
        await ingest.ingest_document(ctx, rid)
    assert await _state(rid) == ("new", 0, 0, "h")
    ctx["job_try"] = ingest.LLM_MAX_TRIES
    assert await ingest.ingest_document(ctx, rid) == "failed"
    assert await _state(rid) == ("failed", 0, 0, None)  # not a source error; refetch next run
