"""`extract()`: one page -> pre-filter -> cache -> `call_llm` (+ verify) -> post-validation.

Goes through `common.llm.call_llm` only, so the cost-cap check can't be bypassed
(AGENTS.md). Raises `NoProviderAvailable` when every extract provider is over cap /
failing; the caller marks the document for retry (doc 08 A7).

Cost controls (doc 08 A7, HE-046):
- keyword pre-filter: no keyword in the model-visible page text -> not a hackathon, no
  LLM call (the 12k truncation keeps every keyword window, so nothing is lost by it);
- content-hash cache: `llm_cache` keyed by sha256(model-visible page text) +
  SCHEMA_VERSION holds the *raw* model output (after verify patches) and the verify review
  reasons. A repeat page makes 0 LLM calls; post-validation re-runs on every hit because
  `status` depends on `now`. Runs where the verify provider was unavailable aren't cached,
  so they get verified next time.
Records with confidence < 0.7 go to the verify provider first (A5/A6, HE-045); the
patched record is then post-validated like any other.
"""

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx2
from redis.asyncio import Redis
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from common.llm import call_llm
from common.models import LlmCache, LlmProviderRole
from workers.extraction.postvalidate import LOW_CONFIDENCE, postvalidate
from workers.extraction.prompt import PageText, build_prompt, page_text
from workers.extraction.schema import EVENTS_MAX, SCHEMA, SCHEMA_VERSION, ExtractionResult
from workers.extraction.verify import verify_event
from workers.keywords import has_keyword

log = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS = 4_000  # 10 events x ~13 capped fields fits well inside this


async def extract(
    engine: AsyncEngine,
    redis: Redis,
    *,
    html: str,
    source_url: str,
    fetched_at: datetime,
    default_tz: str | None = None,
    job_id: str = "",
    now: datetime | None = None,
    client: httpx2.AsyncClient | None = None,
    master_key: bytes | None = None,
) -> ExtractionResult:
    """`default_tz` applies to naive times when the page states none (A4.5: the caller
    passes "Asia/Kolkata" for Indian organisers; None -> UTC + needs_review)."""
    page = page_text(html)
    if not has_keyword(page.text):
        log.info("extraction.prefilter_skip job_id=%s", job_id)
        return ExtractionResult(is_hackathon=False, events=[])
    key = content_hash(page.text)
    run = await cache_get(engine, key)
    if run is None:
        run, provider_id = await _run_llm(
            engine,
            redis,
            page=page,
            source_url=source_url,
            fetched_at=fetched_at,
            job_id=job_id,
            client=client,
            master_key=master_key,
        )
        verified = all("verify_unavailable" not in r for r in run["extra"].values())
        if provider_id is not None and verified:
            await cache_put(engine, key, run, provider_id)
    else:
        log.info("extraction.cache_hit job_id=%s", job_id)
    return postvalidate(
        run["data"],
        page=page,
        source_url=source_url,
        now=now or datetime.now(UTC),
        extractor=run["extractor"],
        default_tz=default_tz,
        extra_reasons={int(i): r for i, r in run["extra"].items()},
    )


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def cache_get(engine: AsyncEngine, key: str) -> dict[str, Any] | None:
    async with AsyncSession(engine) as db:
        row = await db.get(LlmCache, (key, SCHEMA_VERSION))
        return row.result if row else None


async def cache_put(
    engine: AsyncEngine, key: str, run: dict[str, Any], provider_id: uuid.UUID
) -> None:
    async with AsyncSession(engine) as db, db.begin():
        await db.execute(
            insert(LlmCache)
            .values(
                content_hash=key, schema_version=SCHEMA_VERSION, result=run, provider_id=provider_id
            )
            .on_conflict_do_nothing()
        )


async def _run_llm(
    engine: AsyncEngine,
    redis: Redis,
    *,
    page: PageText,
    source_url: str,
    fetched_at: datetime,
    job_id: str,
    client: httpx2.AsyncClient | None,
    master_key: bytes | None,
) -> tuple[dict[str, Any], uuid.UUID | None]:
    """Extract (+ verify low-confidence events). Returns the cacheable run
    {data, extra: {"<event index>": [reasons]}, extractor} and the extract provider id."""
    system, user = build_prompt(source_url, fetched_at, page)
    result = await call_llm(
        engine,
        redis,
        role=LlmProviderRole.extract,
        system=system,
        user=user,
        schema=SCHEMA,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        purpose="extract",
        job_id=job_id,
        client=client,
        master_key=master_key,
    )
    data = result.data  # schema-checked by parse_output: events is a list of dicts
    events = list(data["events"][:EVENTS_MAX]) if data["is_hackathon"] is True else []
    extra: dict[str, list[str]] = {}
    for i, raw in enumerate(events):
        if raw["confidence"] < LOW_CONFIDENCE:
            events[i], extra[str(i)] = await verify_event(
                engine,
                redis,
                raw,
                page=page,
                source_url=source_url,
                job_id=job_id,
                client=client,
                master_key=master_key,
            )
    run = {
        "data": {**data, "events": events},
        "extra": extra,
        "extractor": f"{result.provider}:{result.model}",
    }
    return run, result.provider_id
