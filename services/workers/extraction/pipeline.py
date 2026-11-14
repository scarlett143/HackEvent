"""`extract()`: one page -> one `call_llm` (caps, routing, fallback) -> post-validation.

Goes through `common.llm.call_llm` only, so the cost-cap check can't be bypassed
(AGENTS.md). Raises `NoProviderAvailable` when every provider is over cap / failing; the
caller marks the document for retry (doc 08 A7). Cache + keyword pre-filter: HE-046.
Records with confidence < 0.7 go to the verify provider first (A5/A6, HE-045); the
patched record is then post-validated like any other.
"""

from datetime import UTC, datetime

import httpx2
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from common.llm import call_llm
from common.models import LlmProviderRole
from workers.extraction.postvalidate import LOW_CONFIDENCE, postvalidate
from workers.extraction.prompt import build_prompt, page_text
from workers.extraction.schema import EVENTS_MAX, SCHEMA, ExtractionResult
from workers.extraction.verify import verify_event

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
    extra: dict[int, list[str]] = {}
    for i, raw in enumerate(events):
        if raw["confidence"] < LOW_CONFIDENCE:
            events[i], extra[i] = await verify_event(
                engine,
                redis,
                raw,
                page=page,
                source_url=source_url,
                job_id=job_id,
                client=client,
                master_key=master_key,
            )
    return postvalidate(
        {**data, "events": events},
        page=page,
        source_url=source_url,
        now=now or datetime.now(UTC),
        extractor=f"{result.provider}:{result.model}",
        default_tz=default_tz,
        extra_reasons=extra,
    )
