"""Match a scraped organiser name to a seeded organisation (Fortune / NIRF lists).

The feed shows priority events only (Fortune companies and tier 1 colleges, api/events.py),
and that needs `events.organisation_id`. Scraped organiser names are messy ("Indian
Institute of Technology (IIT), Bombay", "IIT Bombay", "Google", "AWS"), so:

1. normalise both sides: lowercase, drop parenthesised parts, punctuation -> space,
   expand IIT / NIT / IIIT, map brand names to their listed parent (ALIASES);
2. an exact normalised match wins;
3. otherwise the longest organisation name found as a whole-word run inside the organiser
   ("College of Engineering Guindy, Anna University" -> Anna University). Names shorter
   than MIN_CONTAINED characters only match exactly, so short names like "ITC" never match
   by accident.

ponytail: name heuristic. A campus that is not on the list can inherit its parent's tier
(e.g. "VIT Chennai" -> Vellore Institute of Technology).
"""

import re
import uuid
from collections.abc import Iterable

from sqlalchemy import ColumnElement, and_, or_, text
from sqlalchemy.ext.asyncio import AsyncConnection

from common.models import Organisation, OrgKind

MIN_CONTAINED = 8
_PARENS = re.compile(r"\([^)]*\)")
_ABBREV = {
    "iit": "indian institute of technology",
    "iiit": "indian institute of information technology",
    "nit": "national institute of technology",
    "iisc": "indian institute of science",
}
# Brand or subsidiary name -> organisation name as listed in seeds/companies.csv.
ALIASES = {
    "google": "alphabet",
    "google developers": "alphabet",
    "google cloud": "alphabet",
    "aws": "amazon",
    "amazon web services": "amazon",
    "meta": "meta platforms",
    "facebook": "meta platforms",
    "ibm": "international business machines",
    "tcs": "tata consultancy services",
}


def is_priority() -> ColumnElement[bool]:
    """Priority event = organised by a listed (Fortune) company or a tier 1 college.

    Every seeded company comes from Fortune Global 500 / Fortune India 500, so any matched
    company counts; colleges only at tier 1 (NIRF ranks 1-50). Needs an outer join on
    Organisation; an unmatched event is not priority.
    """
    return or_(
        Organisation.kind == OrgKind.company,
        and_(Organisation.kind == OrgKind.institution, Organisation.tier == 1),
    )


def norm(name: str) -> str:
    s = _PARENS.sub(" ", name.lower())
    s = re.sub(r"[^\w\s]|_", " ", s)
    out = " ".join(_ABBREV.get(w, w) for w in s.split())
    return ALIASES.get(out, out)


def best_match(organiser: str, orgs: Iterable[tuple[uuid.UUID, str]]) -> uuid.UUID | None:
    target = norm(organiser)
    if not target:
        return None
    padded = f" {target} "
    best: tuple[int, uuid.UUID] | None = None
    for org_id, name in orgs:
        n = norm(name)
        if n == target:
            return org_id
        if len(n) >= MIN_CONTAINED and f" {n} " in padded and (best is None or len(n) > best[0]):
            best = (len(n), org_id)
    return best[1] if best else None


async def active_orgs(conn: AsyncConnection) -> list[tuple[uuid.UUID, str]]:
    # ponytail: ~600 rows per call; cache it if ingest volume ever makes this show up.
    rows = await conn.execute(text("SELECT id, name FROM organisations WHERE is_active"))
    return [(r.id, r.name) for r in rows]


async def link_unmatched(conn: AsyncConnection) -> int:
    """Backfill: set organisation_id on events that have none and now match. Returns count."""
    orgs = await active_orgs(conn)
    events = await conn.execute(
        text("SELECT id, organiser_name FROM events WHERE organisation_id IS NULL")
    )
    linked = 0
    for ev in events.all():
        org_id = best_match(ev.organiser_name, orgs)
        if org_id is not None:
            await conn.execute(
                text("UPDATE events SET organisation_id = :o WHERE id = :e"),
                {"o": org_id, "e": ev.id},
            )
            linked += 1
    return linked
