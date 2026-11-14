"""Seed organisations from the CSVs next to this file (HE-020, doc 05 §4).

    docker compose exec api python -m seeds.load

Validates every row first and refuses to touch the DB if any row is bad. Then upserts
by `(lower(name), kind)` in one transaction: re-running is a no-op, and an edited CSV
row (tier, region, rank_source, website, name casing) updates the existing row.
`is_active` is set on insert only, so an admin's deactivation survives a re-seed.
See README.md here for sources and the tier rule.
"""

import asyncio
import csv
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from common.config import get_settings
from common.models import Organisation, OrgKind, OrgRegion

SEED_DIR = Path(__file__).resolve().parent
HEADER = ["name", "kind", "region", "tier", "rank_source", "website"]
FILES = {"companies.csv": OrgKind.company, "institutions.csv": OrgKind.institution}


class SeedError(ValueError):
    """One or more CSV rows are invalid; the message lists every problem."""


@dataclass(frozen=True)
class Row:
    name: str
    kind: OrgKind
    region: OrgRegion
    tier: int
    rank_source: str
    website: str  # "" when no verified official site


def _row_errors(rec: dict[str, str], kind: OrgKind) -> list[str]:
    errors = []
    if not rec["name"] or rec["name"] != rec["name"].strip():
        errors.append("name empty or has surrounding whitespace")
    if rec["kind"] != kind.value:
        errors.append(f"kind {rec['kind']!r} must be {kind.value!r} in this file")
    if rec["region"] not in {r.value for r in OrgRegion}:
        errors.append(f"region {rec['region']!r} not one of {[r.value for r in OrgRegion]}")
    if rec["tier"] not in {"1", "2", "3"}:
        errors.append(f"tier {rec['tier']!r} must be 1, 2 or 3")
    if not rec["rank_source"].strip():
        errors.append("rank_source empty")
    url = rec["website"]
    if url:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or "." not in parts.hostname:
            errors.append(f"website {url!r} must be an https URL (or empty)")
    return errors


def read_csv(path: Path, kind: OrgKind) -> list[Row]:
    """Parse and validate one seed file; raise SeedError listing every bad row."""
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, strict=True)
        if reader.fieldnames != HEADER:
            raise SeedError(f"{path.name}: header {reader.fieldnames} must be {HEADER}")
        rows: list[Row] = []
        errors: list[str] = []
        seen: dict[str, str] = {}  # lower(name) -> where first seen
        for rec in reader:
            where = f"{path.name}:{reader.line_num}"
            if None in rec or None in rec.values():
                errors.append(f"{where}: expected {len(HEADER)} columns")
                continue
            if problems := _row_errors(rec, kind):
                errors += [f"{where}: {p}" for p in problems]
                continue
            key = rec["name"].lower()
            if key in seen:
                errors.append(f"{where}: duplicate name {rec['name']!r} (first at {seen[key]})")
                continue
            seen[key] = where
            rows.append(
                Row(
                    rec["name"],
                    kind,
                    OrgRegion(rec["region"]),
                    int(rec["tier"]),
                    rec["rank_source"],
                    rec["website"],
                )
            )
    if errors:
        raise SeedError("\n".join(errors))
    return rows


def read_all(seed_dir: Path = SEED_DIR) -> list[Row]:
    """All organisation rows. sources.csv must be header-only until source adapters land."""
    with (seed_dir / "sources.csv").open(newline="", encoding="utf-8") as f:
        lines = [line for line in csv.reader(f) if line]
    if lines[:1] != [HEADER] or len(lines) > 1:
        raise SeedError("sources.csv: must contain only the header (sources come with HE-024+)")
    return [row for name, kind in FILES.items() for row in read_csv(seed_dir / name, kind)]


async def load(database_url: str, rows: list[Row]) -> dict[str, int]:
    """Upsert `rows`; return organisation counts per kind afterwards."""
    stmt = insert(Organisation).values([asdict(r) | {"is_active": True} for r in rows])
    stmt = stmt.on_conflict_do_update(
        index_elements=[func.lower(Organisation.name), Organisation.kind],
        set_={
            "name": stmt.excluded.name,
            "region": stmt.excluded.region,
            "tier": stmt.excluded.tier,
            "rank_source": stmt.excluded.rank_source,
            "website": stmt.excluded.website,
            "updated_at": func.now(),
        },
    )
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(stmt)
            counts = await conn.execute(
                select(Organisation.kind, func.count()).group_by(Organisation.kind)
            )
            return {kind.value: n for kind, n in counts.all()}
    finally:
        await engine.dispose()


def main() -> None:
    try:
        rows = read_all()
    except SeedError as exc:
        sys.exit(f"Seed files invalid, nothing loaded:\n{exc}")
    counts = asyncio.run(load(get_settings().database_url, rows))
    print(f"Upserted {len(rows)} rows. organisations now: {counts}")


if __name__ == "__main__":
    main()
