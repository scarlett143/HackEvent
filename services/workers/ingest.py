"""Stored page -> event candidates -> `events` (TRD §4.1 step 4, §4.3).

`ingest_document(raw_document_id)` is the one job behind both source paths:

- adapter rss | api | selector: a structured adapter (workers.scrape.adapters) parses the
  body; runs on the scheduler queue, enqueued by `record_scrape`;
- adapter llm: `extract()` (doc 08); runs on worker-llm under the job name `extract`.

Every candidate goes through `upsert_candidate`, the document becomes `parsed`, and the
candidate count feeds source health (`record_candidates`, HE-031). Failures:

- parse error / no parser for the source: document `failed`, counted as a source error
  (the page changed shape: an Admin must look);
- no LLM provider available (caps / outage): retried every LLM_RETRY_S; after the last
  try the document is `failed` and the source's content_hash cleared, so the next
  scheduled scrape stores the page again instead of calling it "unchanged". Not a source
  error: the source is fine.

A document that is no longer `new` is skipped, so a re-delivered job is a no-op. A crash
mid-way leaves it `new`; the arq retry re-runs it (upserts merge, the LLM cache answers).
Imported lazily by workers.settings: worker-scrape must never load DB code.
"""

import logging
import uuid
from typing import Any

from arq import Retry
from sqlalchemy import text

from common.llm import NoProviderAvailable
from workers import scheduler
from workers.dedupe import upsert_candidate
from workers.extraction import extract
from workers.scrape.adapters import EventCandidate, ParseError, UnknownAdapter, parse

log = logging.getLogger(__name__)

LLM_MAX_TRIES = 5  # LlmWorker.max_tries
LLM_RETRY_S = 1800
INDIA_TZ = "Asia/Kolkata"  # doc 08 A4.5: naive times on Indian organisers' pages


def parser_for(adapter: str, selector_config: dict[str, Any] | None) -> str:
    """rss sources use the RSS adapter; api / selector name theirs in selector_config."""
    if adapter == "rss":
        return "rss"
    return str((selector_config or {}).get("parser") or adapter)


async def ingest_document(ctx: dict[str, Any], raw_document_id: str) -> str:
    """Returns "parsed" | "failed" | "skipped"; raises arq Retry while LLMs are unavailable."""
    engine = scheduler.db_engine(ctx)
    async with engine.connect() as conn:
        doc = (
            await conn.execute(
                text(
                    "SELECT d.url, d.body, d.status::text AS status, d.fetched_at,"
                    " s.id::text AS source_id, s.adapter::text AS adapter, s.selector_config,"
                    " o.region::text AS region FROM raw_documents d"
                    " JOIN sources s ON s.id = d.source_id"
                    " LEFT JOIN organisations o ON o.id = s.organisation_id"
                    " WHERE d.id = CAST(:id AS uuid)"
                ),
                {"id": raw_document_id},
            )
        ).first()
    if doc is None or doc.status != "new":
        return "skipped"

    candidates: list[EventCandidate]
    try:
        if doc.adapter == "llm":
            result = await extract(
                engine,
                ctx["redis"],
                html=doc.body,
                source_url=doc.url,
                fetched_at=doc.fetched_at,
                default_tz=INDIA_TZ if doc.region == "india" else None,
                job_id=str(ctx.get("job_id", "")),
            )
            candidates = list(result.events)
        else:
            parser = parser_for(doc.adapter, doc.selector_config)
            candidates = parse(parser, doc.body.encode(), doc.url)
    except (ParseError, UnknownAdapter) as exc:
        await _fail(ctx, raw_document_id, doc.source_id, error=f"{type(exc).__name__}: {exc}")
        return "failed"
    except NoProviderAvailable:
        if ctx.get("job_try", 1) < LLM_MAX_TRIES:
            raise Retry(defer=LLM_RETRY_S) from None
        await _fail(ctx, raw_document_id, doc.source_id, error=None)
        return "failed"

    raw_uuid = uuid.UUID(raw_document_id)
    for cand in candidates:
        await upsert_candidate(engine, cand, source_url=doc.url, raw_document_id=raw_uuid)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE raw_documents SET status = 'parsed' WHERE id = CAST(:id AS uuid)"),
            {"id": raw_document_id},
        )
    await scheduler.record_candidates(ctx, doc.source_id, len(candidates))
    log.info(
        "ingest.parsed raw_document_id=%s adapter=%s candidates=%d",
        raw_document_id,
        doc.adapter,
        len(candidates),
    )
    return "parsed"


async def _fail(ctx: dict[str, Any], raw_id: str, source_id: str, *, error: str | None) -> None:
    """error set: a source error (feeds health). None: LLM unavailable, refetch next run."""
    async with scheduler.db_engine(ctx).begin() as conn:
        await conn.execute(
            text("UPDATE raw_documents SET status = 'failed' WHERE id = CAST(:id AS uuid)"),
            {"id": raw_id},
        )
        if error is None:
            log.warning("ingest.llm_unavailable raw_document_id=%s", raw_id)
            await conn.execute(
                text("UPDATE sources SET content_hash = NULL WHERE id = CAST(:id AS uuid)"),
                {"id": source_id},
            )
            return
        log.warning("ingest.parse_failed raw_document_id=%s error=%s", raw_id, error)
        await conn.execute(
            text(
                "UPDATE sources SET error_streak = error_streak + 1, last_error = :e"
                " WHERE id = CAST(:id AS uuid)"
            ),
            {"id": source_id, "e": error[: scheduler.LAST_ERROR_MAX]},
        )
        await scheduler.update_health(conn, ctx["redis"], source_id)
