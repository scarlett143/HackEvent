"""Source scheduler (HE-021, TRD §4.1 step 1) and scrape-result persistence.

Runs in the `scheduler` container, which holds DB credentials. worker-scrape does not
(Security doc §8), so it hands results back by enqueuing `record_scrape` on the scheduler
queue; this module is the only writer of `sources` change-detection fields and
`raw_documents`. Imported lazily by workers.settings so ScrapeWorker never loads it.
"""

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from common.config import get_settings
from common.queues import LLM_QUEUE, NOTIFY_QUEUE, SCRAPE_QUEUE
from common.source_health import HealthStatus, source_health

log = logging.getLogger(__name__)

TIER_CADENCE_HOURS = {1: 24, 2: 72, 3: 168}  # PRD: daily / every 3 days / weekly
AGGREGATOR_CADENCE_HOURS = 4  # PRD: "aggregators every few hours"
BATCH = 100
LAST_ERROR_MAX = 1000


def cadence_hours_for(kind: str, org_tier: int | None, tier_override: int | None) -> int:
    """Default `sources.cadence_hours` (doc 05: "derived from tier, editable").

    Call it when a source is created or its tier / tier_override changes. The stored
    cadence_hours is the truth the scheduler reads, so an Admin edit sticks until the tier
    changes again. Aggregators (organisation_id null) poll every few hours; any other
    source without a tier is treated as tier 3, the politest cadence.
    """
    if kind == "aggregator":
        return AGGREGATOR_CADENCE_HOURS
    tier = tier_override if tier_override is not None else org_tier
    return TIER_CADENCE_HOURS[tier if tier is not None else 3]


def scrape_payload(
    source_id: str,
    url: str,
    etag: str | None,
    last_modified: str | None,
    content_hash: str | None,
    kind: str,
) -> dict[str, Any]:
    """The `scrape_source` argument; the scheduler and Admin "Run now" (HE-091) share it."""
    return {
        "id": source_id,
        "url": url,
        "etag": etag,
        "last_modified": last_modified,
        "content_hash": content_hash,
        "respect_robots": kind != "social",  # TRD §4.1: robots for non-social
    }


def _engine(ctx: dict[str, Any]) -> AsyncEngine:
    if "db" not in ctx:
        ctx["db"] = create_async_engine(get_settings().database_url, pool_size=2)
    engine: AsyncEngine = ctx["db"]
    return engine


async def schedule_due(ctx: dict[str, Any]) -> int:
    """Cron, every minute: enqueue `scrape_source` for due, enabled sources.

    SKIP LOCKED lets two schedulers run without taking the same rows; next_run_at is moved
    forward in the same transaction (from now, plus up to 10 min jitter) so a crash cannot
    re-enqueue in a loop. The arq job id is (source, due slot), so even a commit that fails
    after enqueuing cannot produce a second job for the same slot.
    """
    redis = ctx["redis"]
    async with _engine(ctx).begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT id::text, url, etag, last_modified, content_hash, kind::text,"
                    " next_run_at FROM sources WHERE is_enabled AND next_run_at <= now()"
                    " ORDER BY next_run_at LIMIT :n FOR UPDATE SKIP LOCKED"
                ),
                {"n": BATCH},  # ponytail: 100/min ceiling; ~400 jobs/day fits easily
            )
        ).all()
        if not rows:
            return 0
        await conn.execute(
            text(
                "UPDATE sources SET next_run_at = now() + make_interval(hours => cadence_hours)"
                " + random() * interval '10 minutes' WHERE id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": [r.id for r in rows]},
        )
        for r in rows:
            await redis.enqueue_job(
                "scrape_source",
                scrape_payload(r.id, r.url, r.etag, r.last_modified, r.content_hash, r.kind),
                _job_id=f"scrape:{r.id}:{r.next_run_at.isoformat()}",
                _queue_name=SCRAPE_QUEUE,
            )
    return len(rows)


async def record_scrape(ctx: dict[str, Any], result: dict[str, Any]) -> str:
    """Persist one `scrape_source` result (see workers.scrape.job for the keys).

    error → error_streak + 1 and last_error; success resets them (HE-031 health). Otherwise refresh the
    conditional-GET validators; if the hash differs from the stored one, store a
    raw_documents row and, for adapter=llm, enqueue `extract(raw_document_id)`.
    """
    raw_id: str | None = None
    async with _engine(ctx).begin() as conn:
        source = (
            await conn.execute(
                text(
                    "SELECT adapter::text, content_hash FROM sources"
                    " WHERE id = CAST(:id AS uuid) FOR UPDATE"
                ),
                {"id": result["source_id"]},
            )
        ).first()
        if source is None:
            return "gone"
        if result["outcome"] == "error":
            await conn.execute(
                text(
                    "UPDATE sources SET error_streak = error_streak + 1, last_error = :e"
                    " WHERE id = CAST(:id AS uuid)"
                ),
                {"id": result["source_id"], "e": result["error"][:LAST_ERROR_MAX]},
            )
            await _update_health(conn, ctx["redis"], result["source_id"])
            return "error"
        changed = result["outcome"] == "changed" and result["content_hash"] != source.content_hash
        await conn.execute(
            text(
                "UPDATE sources SET etag = COALESCE(:etag, etag),"
                " last_modified = COALESCE(:lm, last_modified),"
                " content_hash = COALESCE(:h, content_hash),"
                " last_success_at = now(), error_streak = 0, last_error = NULL"
                " WHERE id = CAST(:id AS uuid)"
            ),
            {
                "id": result["source_id"],
                "etag": result["etag"],
                "lm": result["last_modified"],
                "h": result["content_hash"],
            },
        )
        await _update_health(conn, ctx["redis"], result["source_id"])
        if not changed:
            return "unchanged"  # TRD §4.1 step 3: same content as last run, stop
        raw_id = (
            await conn.execute(
                text(
                    "INSERT INTO raw_documents (source_id, url, content_hash, body, status)"
                    " VALUES (CAST(:id AS uuid), :url, :h, :body, 'new') RETURNING id::text"
                ),
                {
                    "id": result["source_id"],
                    "url": result["final_url"],
                    "h": result["content_hash"],
                    "body": result["body"],
                },
            )
        ).scalar_one()
    # After commit: a failed enqueue leaves a `new` row to re-drive, never a dangling job.
    if source.adapter == "llm":
        # worker-llm implements `extract` later (doc 08); until then arq logs it unknown.
        await ctx["redis"].enqueue_job(
            "extract", raw_id, _job_id=f"extract:{raw_id}", _queue_name=LLM_QUEUE
        )
    # HE-023/024 adapters hook in here: rss | api | selector parse the stored document.
    return "changed"


async def record_candidates(ctx: dict[str, Any], source_id: str, count: int) -> str:
    """Report how many event candidates one run of `source_id` produced (HE-031).

    Called by the adapter (HE-023/024) and extraction (HE-044) paths once per changed
    document: 0 → empty_streak + 1, otherwise reset. Returns the new health status.
    """
    async with _engine(ctx).begin() as conn:
        found = (
            await conn.execute(
                text(
                    "UPDATE sources SET empty_streak ="
                    " CASE WHEN :n = 0 THEN empty_streak + 1 ELSE 0 END"
                    " WHERE id = CAST(:id AS uuid) RETURNING id"
                ),
                {"id": source_id, "n": count},
            )
        ).first()
        if found is None:
            return "gone"
        return await _update_health(conn, ctx["redis"], source_id)


async def _update_health(conn: AsyncConnection, redis: Any, source_id: str) -> HealthStatus:
    """Alert all active Admins once when a source turns red; re-arm when it is green.

    `health_alerted_at` is the alerted state: set on the transition into red, cleared only
    on green, so a source that stays red (or dips to amber) is not re-alerted. Enqueued
    inside the caller's transaction: a failed enqueue rolls back and the next run retries
    (at-least-once; the job id makes a retried enqueue of the same alert a no-op).
    """
    row = (
        await conn.execute(
            text(
                "SELECT s.error_streak, s.empty_streak, s.last_error, s.health_alerted_at,"
                " COALESCE(o.name, s.url) AS name FROM sources s"
                " LEFT JOIN organisations o ON o.id = s.organisation_id"
                " WHERE s.id = CAST(:id AS uuid)"
            ),
            {"id": source_id},
        )
    ).one()
    health = source_health(row.error_streak, row.empty_streak, row.last_error)
    if health.status == HealthStatus.GREEN and row.health_alerted_at is not None:
        await conn.execute(
            text("UPDATE sources SET health_alerted_at = NULL WHERE id = CAST(:id AS uuid)"),
            {"id": source_id},
        )
    elif health.status == HealthStatus.RED and row.health_alerted_at is None:
        alerted_at = (
            await conn.execute(
                text(
                    "UPDATE sources SET health_alerted_at = now()"
                    " WHERE id = CAST(:id AS uuid) RETURNING health_alerted_at"
                ),
                {"id": source_id},
            )
        ).scalar_one()
        admins = (
            await conn.execute(
                text("SELECT email FROM users WHERE role = 'admin' AND is_active ORDER BY email")
            )
        ).scalars()
        log.warning(
            "source.health_red source_id=%s reason=%s",
            source_id,
            health.reason,
            extra={"event": "source.health_red", "source_id": source_id},
        )
        for email in admins:
            await redis.enqueue_job(
                "send_email",
                email,
                "source_health_red",
                {"source_name": row.name, "reason": health.reason},
                _job_id=f"health_red:{source_id}:{alerted_at.isoformat()}:{email}",
                _queue_name=NOTIFY_QUEUE,
            )
    return health.status
