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

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from common.config import get_settings
from common.fetch import ssrf
from common.models import Source, SourceAdapter, SourceKind
from workers.scheduler import cadence_hours_for

_UNSTOP = "https://unstop.com/api/public/opportunity/search-result"
_DEVPOST = "https://devpost.com/api/hackathons?status[]=upcoming&status[]=open"
# Owner list (2026-11-19): big-tech organisers the feed must carry. Devpost's `search`
# matches the organiser; only names whose search is exact (meta.fuzzy=false) are listed.
DEVPOST_COMPANIES = ("aws", "google", "microsoft", "ibm", "meta", "nvidia", "openai")
# Switched off on load: DoraHacks answers every bot request (robots.txt included) with an
# AWS WAF human-verification page (HTTP 405), so it can only fail. We do not evade bot walls.
RETIRED = ("https://dorahacks.io/hackathon", "https://lablab.ai/event")

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
    # Feed calibration (≥ 75 hackathons): each source is one page, so add the next pages.
    # Unstop: 10 per page keeps a page under the 200 KB raw-body cap (169 open on 2026-11-19);
    # Devpost is fixed at 9 per page.
    *(
        (
            f"{_UNSTOP}?opportunity=hackathons&oppstatus=open&per_page=10&page={n}",
            SourceAdapter.api,
            {"parser": "unstop"},
        )
        for n in range(1, 11)
    ),
    *(
        (
            f"https://devpost.com/api/hackathons?status[]=upcoming&status[]=open&page={n}",
            SourceAdapter.api,
            {"parser": "devpost"},
        )
        for n in (2, 3, 4, 5)
    ),
    *(
        (f"{_DEVPOST}&search={q}", SourceAdapter.api, {"parser": "devpost"})
        for q in DEVPOST_COMPANIES
    ),
    # AMD / NVIDIA / Qualcomm run their contests here (robots.txt allows /contests).
    ("https://www.hackster.io/contests", SourceAdapter.llm, None),
    ("https://devfolio.co/hackathons", SourceAdapter.llm, None),
    ("https://www.mlh.com/seasons/2027/events", SourceAdapter.api, {"parser": "mlh"}),
    ("https://www.hackerearth.com/challenges/hackathon/", SourceAdapter.llm, None),
    ("https://hack2skill.com/", SourceAdapter.llm, None),
    # lablab.ai moved here (OpenAI / Anthropic hackathons); the list is rendered client-side.
    ("https://starfieldlabs.ai/ai-hackathons", SourceAdapter.llm, {"render": True}),
    ("https://taikai.network/hackathons", SourceAdapter.llm, None),
    ("https://www.hackathon.com/online", SourceAdapter.llm, None),
]


async def load(database_url: str) -> dict[str, int]:
    """Insert the platforms that are not there yet and switch off RETIRED ones.
    Returns {"added": n, "skipped": m, "retired": r}."""
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
            retired = len(
                (
                    await db.scalars(
                        update(Source)
                        .where(Source.url.in_(RETIRED), Source.is_enabled)
                        .values(is_enabled=False)
                        .returning(Source.id)
                    )
                ).all()
            )
    finally:
        await engine.dispose()
    return {"added": added, "skipped": skipped, "retired": retired}


def main() -> None:
    print(asyncio.run(load(get_settings().database_url)))


if __name__ == "__main__":
    main()
