"""Dedupe + upsert of event candidates (TRD §4.3, HE-047).

One entry point, `upsert_candidate`, for every source (structured adapters and the LLM
extractor emit the same `EventCandidate`):

1. Exact: `dedupe_key = sha256(normalise(title) | normalise(organiser) | event_start::date)`
   (date in UTC, '' when unknown). Normalise = lowercase, drop year / edition tokens
   ("2026", "season 3", "5th edition"), punctuation -> space, collapse whitespace.
2. Fuzzy (no exact hit): pg_trgm `similarity(title) > 0.8`, same normalised organiser,
   and event_start or registration_deadline within 3 days. Score >= AUTO_MERGE (0.9)
   -> merge; otherwise the candidate becomes its own event with review_status
   needs_review, so an admin can merge it in the review queue (HE-093).
3. Merge only *fills gaps*: empty fields take the candidate's value, eligibility chips are
   unioned. A different deadline from a trusted (not needs_review) candidate replaces the
   old one and stamps `deadline_changed_at` (reminder recompute is HE-072). Admin
   decisions (approved / rejected) and `cancelled` are never overwritten.
4. Every call upserts the `event_sources` row (event, source_url) with the evidence.

Concurrency: the exact-key insert is `ON CONFLICT DO NOTHING`; losing a race falls back to
merging into the winner's row (locked `FOR UPDATE`), so one key never yields two events.
"""

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import ColumnElement, and_, false, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from common.models import Event, EventMode, EventSource, EventStatus, ReviewStatus
from workers.scrape.adapters.base import EventCandidate

FUZZY_MIN = 0.8  # TRD §4.3: merge candidate above this
AUTO_MERGE = 0.9  # TRD: "auto-merge if score high"; between the two -> needs_review
DATE_WINDOW = timedelta(days=3)
ELIGIBILITY_MAX = 10

Outcome = Literal["created", "created_review", "merged_exact", "merged_fuzzy"]

_EDITION = re.compile(
    r"\b(?:19|20)\d{2}(?:\s+(?:edition|season))?\b"  # years, "2027 edition"
    r"|\b(?:season|edition|ed|v|version|chapter)\s*\d+\b"  # season 3, v2
    r"|\b\d+(?:st|nd|rd|th)\s+(?:edition|season|annual)\b",  # 5th edition
    re.IGNORECASE,
)


def normalise(value: str) -> str:
    s = _EDITION.sub(" ", value.lower())
    s = re.sub(r"[^\w\s]|_", " ", s)
    return " ".join(s.split())


def dedupe_key(title: str, organiser: str, event_start: datetime | None) -> str:
    day = event_start.astimezone(UTC).date().isoformat() if event_start else ""
    return hashlib.sha256(f"{normalise(title)}|{normalise(organiser)}|{day}".encode()).hexdigest()


@dataclass(frozen=True)
class Upsert:
    event_id: uuid.UUID
    outcome: Outcome
    score: float | None = None  # fuzzy similarity, when the fuzzy pass decided


def _needs_review(cand: EventCandidate) -> bool:
    return bool(getattr(cand, "needs_review", False))  # ExtractedCandidate (LLM) only


def _status(deadline: datetime | None, now: datetime) -> EventStatus:
    return EventStatus.closed if deadline and deadline < now else EventStatus.open


def _near(
    column: InstrumentedAttribute[datetime | None], value: datetime | None
) -> ColumnElement[bool]:
    if value is None:
        return false()
    return and_(column.is_not(None), column.between(value - DATE_WINDOW, value + DATE_WINDOW))


async def _fuzzy(db: AsyncSession, cand: EventCandidate) -> tuple[Event, float] | None:
    score = func.similarity(Event.title, cand.title)
    rows = (
        await db.execute(
            select(Event, score)
            .where(
                score > FUZZY_MIN,
                or_(
                    _near(Event.event_start, cand.event_start),
                    _near(Event.registration_deadline, cand.registration_deadline),
                ),
            )
            .order_by(score.desc(), Event.first_seen_at)
            .limit(20)
        )
    ).all()
    org = normalise(cand.organiser)
    for event, s in rows:
        if normalise(event.organiser_name) == org:
            return event, float(s)
    return None


def _merge(ev: Event, cand: EventCandidate, now: datetime) -> None:
    for field in ("description", "registration_url", "location", "prize"):
        value = getattr(cand, field)
        if value and not getattr(ev, field):
            setattr(ev, field, value)
    for field in ("event_start", "event_end"):
        if getattr(ev, field) is None and getattr(cand, field) is not None:
            setattr(ev, field, getattr(cand, field))
    if ev.mode == EventMode.unknown and cand.mode != "unknown":
        ev.mode = EventMode(cand.mode)
    ev.eligibility = list(dict.fromkeys([*ev.eligibility, *cand.eligibility]))[:ELIGIBILITY_MAX]
    new = cand.registration_deadline
    if new is not None and ev.registration_deadline is None:
        ev.registration_deadline = new
    elif new is not None and new != ev.registration_deadline and not _needs_review(cand):
        ev.registration_deadline, ev.deadline_changed_at = new, now
    ev.confidence = max(ev.confidence, _confidence(cand))
    if ev.status != EventStatus.cancelled:
        ev.status = _status(ev.registration_deadline, now)


def _confidence(cand: EventCandidate) -> Decimal:
    return Decimal(str(round(cand.confidence, 2)))


async def _link_source(
    db: AsyncSession,
    event_id: uuid.UUID,
    cand: EventCandidate,
    source_url: str,
    raw_document_id: uuid.UUID | None,
) -> None:
    values = {
        "raw_document_id": raw_document_id,
        "snippet": cand.evidence,
        "extracted": cand.model_dump(mode="json"),
        "extractor": cand.extractor,
    }
    await db.execute(
        insert(EventSource)
        .values(event_id=event_id, source_url=source_url, **values)
        .on_conflict_do_update(index_elements=["event_id", "source_url"], set_=values)
    )


async def upsert_candidate(
    engine: AsyncEngine,
    cand: EventCandidate,
    *,
    source_url: str,
    raw_document_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> Upsert:
    """Dedupe one candidate into `events` + link its `event_sources` row (module doc)."""
    now = now or datetime.now(UTC)
    key = dedupe_key(cand.title, cand.organiser, cand.event_start)
    async with AsyncSession(engine) as db, db.begin():
        by_key = select(Event).where(Event.dedupe_key == key).with_for_update()
        ev = await db.scalar(by_key)
        outcome: Outcome = "merged_exact"
        score: float | None = None
        review = _needs_review(cand)
        if ev is None:
            match = await _fuzzy(db, cand)
            if match is not None:
                score = match[1]
                if score >= AUTO_MERGE:
                    ev = await db.scalar(
                        select(Event).where(Event.id == match[0].id).with_for_update()
                    )
                    outcome = "merged_fuzzy"
                else:
                    review = True  # possible duplicate: an admin decides (HE-093)
        if ev is None:
            event_id = await db.scalar(
                insert(Event)
                .values(
                    title=cand.title,
                    organiser_name=cand.organiser,
                    description=cand.description or "",
                    # '' = unknown, so a later source can fill it; UI falls back to event_sources
                    registration_url=cand.registration_url or "",
                    registration_deadline=cand.registration_deadline,
                    event_start=cand.event_start,
                    event_end=cand.event_end,
                    mode=EventMode(cand.mode),
                    location=cand.location,
                    eligibility=cand.eligibility,
                    prize=cand.prize,
                    status=_status(cand.registration_deadline, now),
                    review_status=ReviewStatus.needs_review if review else ReviewStatus.auto,
                    confidence=_confidence(cand),
                    dedupe_key=key,
                    first_seen_at=now,
                )
                .on_conflict_do_nothing(index_elements=["dedupe_key"])
                .returning(Event.id)
            )
            if event_id is not None:
                await _link_source(db, event_id, cand, source_url, raw_document_id)
                return Upsert(event_id, "created_review" if review else "created", score)
            ev = await db.scalar(by_key)  # lost the insert race: merge into the winner
            assert ev is not None
            outcome = "merged_exact"
        _merge(ev, cand, now)
        await db.flush()
        await _link_source(db, ev.id, cand, source_url, raw_document_id)
        return Upsert(ev.id, outcome, score)
