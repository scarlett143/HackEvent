"""HE-047: dedupe (exact key + pg_trgm fuzzy). Acceptance: duplicate fixtures merge.

Stack tests need a THROWAWAY Postgres (see tests/stack.py); `events` is truncated.
"""

import asyncio
import json
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from tests.stack import ALEMBIC_INI, DB_URL, needs_stack, sql

from common.models import Event, EventSource, ReviewStatus
from workers.dedupe import dedupe_key, normalise, upsert_candidate
from workers.extraction.schema import ExtractedCandidate
from workers.scrape.adapters.base import EventCandidate

FIXTURE = json.loads((Path(__file__).parent / "fixtures/dedupe/candidates.json").read_text())
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
START = datetime(2027, 2, 20, 4, 30, tzinfo=UTC)


# --- unit --------------------------------------------------------------------------------


def test_normalise_drops_years_editions_punctuation() -> None:
    assert normalise("CodeSprint 2027") == "codesprint"
    assert normalise("CodeSprint — Season 3!") == "codesprint"
    assert normalise("codesprint (5th edition)") == "codesprint"
    assert normalise("codesprint (2027 edition)") == "codesprint"
    assert normalise("  Example   University. ") == "example university"
    assert normalise("Hack_Week v2") == "hack week"


def test_dedupe_key_uses_utc_start_day() -> None:
    k = dedupe_key("CodeSprint 2027", "Example University", START)
    assert k == dedupe_key("codesprint", "EXAMPLE UNIVERSITY", START + timedelta(hours=10))
    assert k != dedupe_key("codesprint", "example university", START + timedelta(days=1))
    assert k != dedupe_key("codesprint", "example university", None)
    assert len(k) == 64


# --- stack -------------------------------------------------------------------------------


@pytest.fixture
def stack() -> None:
    assert DB_URL
    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["skip_logging"] = True
    cfg.attributes["database_url"] = DB_URL
    command.upgrade(cfg, "head")
    sql("TRUNCATE events CASCADE")


@pytest_asyncio.fixture
async def engine(stack: None) -> AsyncIterator[AsyncEngine]:
    assert DB_URL
    eng = create_async_engine(DB_URL, pool_size=10)
    yield eng
    await eng.dispose()


async def events(engine: AsyncEngine) -> list[Event]:
    async with AsyncSession(engine) as db:
        return list((await db.scalars(select(Event).order_by(Event.first_seen_at))).all())


async def sources(engine: AsyncEngine, event_id: Any) -> list[EventSource]:
    async with AsyncSession(engine) as db:
        rows = await db.scalars(select(EventSource).where(EventSource.event_id == event_id))
        return list(rows.all())


def cand(**over: Any) -> EventCandidate:
    base = {
        "title": "CodeSprint 2027",
        "organiser": "Example University",
        "event_start": START,
        "registration_deadline": datetime(2027, 2, 6, 18, 29, 59, tzinfo=UTC),
        "confidence": 0.9,
        "extractor": "devpost",
    }
    return EventCandidate.model_validate(base | over)


@needs_stack
async def test_acceptance_duplicate_fixtures_merge(engine: AsyncEngine) -> None:
    ids: dict[str, set[Any]] = defaultdict(set)
    outcomes: dict[str, list[str]] = defaultdict(list)
    for i, item in enumerate(FIXTURE["candidates"]):
        c = EventCandidate.model_validate(item["candidate"])
        up = await upsert_candidate(
            engine, c, source_url=item["source_url"], now=NOW + timedelta(seconds=i)
        )
        ids[item["group"]].add(up.event_id)
        outcomes[item["group"]].append(up.outcome)

    assert {g: len(s) for g, s in ids.items()} == FIXTURE["expected"]
    assert outcomes["exact"] == ["created", "merged_exact", "merged_exact"]
    assert outcomes["fuzzy_auto"] == ["created", "merged_fuzzy"]
    assert outcomes["fuzzy_review"] == ["created", "created_review"]
    assert len(await events(engine)) == sum(FIXTURE["expected"].values())

    # the exact group's gaps were filled from later sources; first-seen values kept
    (code_id,) = ids["exact"]
    by_id = {e.id: e for e in await events(engine)}
    ev = by_id[code_id]
    assert ev.title == "CodeSprint 2027" and ev.location == "Coimbatore"
    assert ev.registration_url == "https://example.edu/codesprint/register"
    assert ev.mode.value == "offline" and ev.eligibility == ["students", "faculty"]
    assert ev.event_end is not None and ev.prize == "INR 1,00,000"
    assert ev.deadline_changed_at is None  # same deadline from every source
    assert {s.extractor for s in await sources(engine, code_id)} == {
        "devpost",
        "gemini:gemini-flash",
        "rss",
    }
    review = [by_id[i] for i in ids["fuzzy_review"]]
    assert sorted(e.review_status for e in review) == [ReviewStatus.auto, ReviewStatus.needs_review]


@needs_stack
async def test_trusted_deadline_change_is_applied_and_stamped(engine: AsyncEngine) -> None:
    first = await upsert_candidate(engine, cand(), source_url="https://a.example/x", now=NOW)
    moved = datetime(2027, 2, 10, 18, 29, 59, tzinfo=UTC)
    await upsert_candidate(
        engine, cand(registration_deadline=moved), source_url="https://b.example/x", now=NOW
    )
    (ev,) = await events(engine)
    assert ev.id == first.event_id
    assert ev.registration_deadline == moved and ev.deadline_changed_at == NOW


@needs_stack
async def test_needs_review_candidate_cannot_move_the_deadline(engine: AsyncEngine) -> None:
    original = cand().registration_deadline
    await upsert_candidate(engine, cand(), source_url="https://a.example/x", now=NOW)
    shady = ExtractedCandidate(
        **cand(registration_deadline=datetime(2027, 1, 1, tzinfo=UTC)).model_dump()
        | {"evidence": "Apply by 1 January 2027."},
        needs_review=True,
        review_reasons=["evidence_not_in_page"],
    )
    up = await upsert_candidate(engine, shady, source_url="https://b.example/x", now=NOW)
    (ev,) = await events(engine)
    assert up.outcome == "merged_exact"
    assert ev.registration_deadline == original and ev.deadline_changed_at is None


@needs_stack
async def test_past_deadline_closes_and_same_source_is_one_row(engine: AsyncEngine) -> None:
    later = NOW + timedelta(days=200)
    a = await upsert_candidate(engine, cand(), source_url="https://a.example/x", now=NOW)
    await upsert_candidate(engine, cand(), source_url="https://a.example/x", now=later)
    (ev,) = await events(engine)
    assert ev.status.value == "closed"
    assert len(await sources(engine, a.event_id)) == 1


@needs_stack
async def test_concurrent_upserts_of_one_key_make_one_event(engine: AsyncEngine) -> None:
    results = await asyncio.gather(
        *(
            upsert_candidate(engine, cand(), source_url=f"https://s{i}.example/x", now=NOW)
            for i in range(6)
        )
    )
    assert len({r.event_id for r in results}) == 1
    assert [r.outcome for r in results].count("created") == 1
    assert len(await events(engine)) == 1
    assert len(await sources(engine, results[0].event_id)) == 6
