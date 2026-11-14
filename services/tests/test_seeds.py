"""Seed CSVs + loader (HE-020).

CSV checks run everywhere. The DB test needs a THROWAWAY Postgres 16 (it migrates to head
and truncates `organisations` CASCADE) and is skipped unless TEST_DATABASE_URL is set; see
tests/stack.py for the docker command.
"""

import asyncio
import dataclasses
import os
import re
from collections import Counter
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from tests.stack import ALEMBIC_INI, sql

from common.models import OrgKind, OrgRegion
from seeds.load import HEADER, SEED_DIR, SeedError, load, read_all, read_csv

RANK = re.compile(
    r"^(?:Fortune Global 500 2026|Fortune India 500 2025|NIRF 2025 Engineering)"
    r" (?:#(\d+)|band (\d+)-\d+)$"
)


def expected_tier(rank_source: str) -> int:
    m = RANK.match(rank_source)
    assert m, rank_source
    pos = int(m.group(1) or m.group(2))
    return 1 if pos <= 50 else 2 if pos <= 150 else 3


def test_seed_files_have_exactly_300_companies_and_300_institutions() -> None:
    rows = read_all()
    assert Counter(r.kind for r in rows) == {OrgKind.company: 300, OrgKind.institution: 300}
    companies = [r for r in rows if r.kind is OrgKind.company]
    assert Counter(r.region for r in companies) == {OrgRegion.global_: 150, OrgRegion.india: 150}
    for kind in OrgKind:
        names = [r.name.lower() for r in rows if r.kind is kind]
        assert len(set(names)) == len(names)


def test_every_row_has_a_valid_tier_source_and_website() -> None:
    for r in read_all():
        assert r.tier == expected_tier(r.rank_source), r
        assert r.website == "" or re.fullmatch(r"https://[a-z0-9.-]+\.[a-z]+(/\S*)?", r.website), r
        if r.rank_source.startswith("Fortune India") or r.kind is OrgKind.institution:
            assert r.region is OrgRegion.india, r
        else:
            assert r.region is OrgRegion.global_, r


def write(path: Path, *lines: str) -> Path:
    path.write_text("\n".join([",".join(HEADER), *lines]) + "\n", encoding="utf-8")
    return path


def test_bad_rows_fail_loudly_with_every_problem_listed(tmp_path: Path) -> None:
    f = write(
        tmp_path / "companies.csv",
        "Acme,company,india,1,List 2025 #1,https://acme.example",
        "acme,company,india,1,List 2025 #2,https://acme2.example",
        "Bad Tier,company,india,4,List 2025 #3,",
        "Bad Region,company,europe,1,List 2025 #4,",
        "Plain Http,company,india,1,List 2025 #5,http://plain.example",
        "Wrong Kind,institution,india,1,List 2025 #6,",
        "No Source,company,india,1,,",
        "Extra,company,india,1,List 2025 #7,,surplus",
    )
    with pytest.raises(SeedError) as exc:
        read_csv(f, OrgKind.company)
    msg = str(exc.value)
    for needle in (
        "companies.csv:3: duplicate name 'acme'",
        ":4: tier '4'",
        ":5: region 'europe'",
        ":6: website 'http://plain.example'",
        ":7: kind 'institution'",
        ":8: rank_source empty",
        ":9: expected 6 columns",
    ):
        assert needle in msg


def test_wrong_header_and_non_empty_sources_are_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "companies.csv"
    bad.write_text("name,kind\nAcme,company\n", encoding="utf-8")
    with pytest.raises(SeedError, match="header"):
        read_csv(bad, OrgKind.company)
    for name in ("companies.csv", "institutions.csv"):
        (tmp_path / name).write_bytes((SEED_DIR / name).read_bytes())
    write(tmp_path / "sources.csv", "Devpost,aggregator,global,1,x,https://devpost.com")
    with pytest.raises(SeedError, match="sources.csv"):
        read_all(tmp_path)


# --- real Postgres ---

DB_URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")


@needs_db
def test_load_is_idempotent_and_csv_edits_update_rows() -> None:
    assert DB_URL
    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["skip_logging"] = True
    cfg.attributes["database_url"] = DB_URL
    command.upgrade(cfg, "head")
    sql("TRUNCATE organisations CASCADE")
    rows = read_all()
    expected = {"company": 300, "institution": 300}

    assert asyncio.run(load(DB_URL, rows)) == expected
    first = dict(sql("SELECT id::text, updated_at FROM organisations"))
    target = rows[0]
    sql("UPDATE organisations SET is_active = false WHERE name = :n", n=target.name)

    assert asyncio.run(load(DB_URL, rows)) == expected  # second run: no new rows
    assert set(dict(sql("SELECT id::text, updated_at FROM organisations"))) == set(first)

    edited = dataclasses.replace(target, name=target.name.upper(), tier=target.tier % 3 + 1)
    assert asyncio.run(load(DB_URL, [edited, *rows[1:]])) == expected
    assert sql(
        "SELECT name, tier, is_active FROM organisations WHERE lower(name) = lower(:n)",
        n=target.name,
    ) == [(edited.name, edited.tier, False)]  # updated in place; deactivation kept
