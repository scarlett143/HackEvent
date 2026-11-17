"""Major hackathon platforms as crawl sources (kind=aggregator).

Run once per environment:  docker compose exec api python -m seeds.platforms

- Idempotent: a URL that already exists is skipped, so admin edits are never overwritten.
- Every URL passes the SSRF guard (doc 07 §8) before it is stored.
- Cadence comes from the scheduler's own rule for aggregators (cadence_hours_for).
- Devpost and Unstop use their dedicated API parsers; the other platforms use the generic LLM
  page adapter, so their extraction quality depends on how much of each page is server-rendered.
  URLs were checked to answer HTTP 200 on 2026-11-17.
"""

import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from common.config import get_settings
from common.fetch import ssrf
from common.models import Source, SourceAdapter, SourceKind
from workers.scheduler import cadence_hours_for

# (url, adapter, selector_config)
PLATFORMS: list[tuple[str, SourceAdapter, dict[str, Any] | None]] = [
    (
        "https://devpost.com/api/hackathons?status[]=upcoming&status[]=open",
        SourceAdapter.api,
        {"parser": "devpost"},
    ),
    (
        "https://unstop.com/api/public/opportunity/search-result",
        SourceAdapter.api,
        {"parser": "unstop"},
    ),
    ("https://devfolio.co/hackathons", SourceAdapter.llm, None),
    ("https://mlh.io/seasons/2027/events", SourceAdapter.llm, None),
    ("https://www.hackerearth.com/challenges/hackathon/", SourceAdapter.llm, None),
    ("https://hack2skill.com/", SourceAdapter.llm, None),
    ("https://dorahacks.io/hackathon", SourceAdapter.llm, None),
    ("https://lablab.ai/event", SourceAdapter.llm, None),
    ("https://taikai.network/hackathons", SourceAdapter.llm, None),
    ("https://www.hackathon.com/online", SourceAdapter.llm, None),
]


async def load(database_url: str) -> dict[str, int]:
    """Insert the platforms that are not there yet. Returns {"added": n, "skipped": m}."""
    for url, _, _ in PLATFORMS:
        await ssrf.check_url(url)  # raises ssrf.BlockedURL: nothing is written if any URL fails
    engine = create_async_engine(database_url)
    added = skipped = 0
    try:
        async with AsyncSession(engine) as db, db.begin():
            existing = set(
                await db.scalars(
                    select(Source.url).where(Source.url.in_([u for u, _, _ in PLATFORMS]))
                )
            )
            for url, adapter, config in PLATFORMS:
                if url in existing:
                    skipped += 1
                    continue
                db.add(
                    Source(
                        kind=SourceKind.aggregator,
                        url=url,
                        adapter=adapter,
                        selector_config=config,
                        cadence_hours=cadence_hours_for(SourceKind.aggregator.value, None, None),
                        is_enabled=True,
                    )
                )
                added += 1
    finally:
        await engine.dispose()
    return {"added": added, "skipped": skipped}


def main() -> None:
    print(asyncio.run(load(get_settings().database_url)))


if __name__ == "__main__":
    main()
