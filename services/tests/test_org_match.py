"""Organiser name -> seeded organisation (common.org_match), against the real seed lists."""

import csv
import uuid
from pathlib import Path

import pytest

from common.org_match import best_match, norm

SEEDS = Path(__file__).resolve().parents[1] / "seeds"
ORGS = {
    row["name"]: uuid.uuid5(uuid.NAMESPACE_URL, row["name"])
    for f in ("companies.csv", "institutions.csv")
    for row in csv.DictReader((SEEDS / f).open())
}
BY_ID = {v: k for k, v in ORGS.items()}


def match(organiser: str) -> str | None:
    found = best_match(organiser, [(i, n) for n, i in ORGS.items()])
    return BY_ID[found] if found else None


@pytest.mark.parametrize(
    ("organiser", "expected"),
    [
        # Names seen on real aggregator pages (local crawl, 2026-11-18).
        ("Indian Institute of Technology (IIT), Bombay", "Indian Institute of Technology Bombay"),
        ("IIT Bombay", "Indian Institute of Technology Bombay"),
        ("College of Engineering (CEG), Guindy, Anna University", "Anna University"),
        ("Chandigarh University (CU), Ajitgarh, Punjab", "Chandigarh University"),
        (
            "Madan Mohan Malaviya University of Technology",
            "Madan Mohan Malaviya University of Technology",
        ),
        ("Google", "Alphabet"),
        ("AWS", "Amazon"),
        ("Meta", "Meta Platforms"),
        ("Infosys", "Infosys"),
        ("TCS", "Tata Consultancy Services"),
        # Not on any list.
        ("Hack4TodayOrg", None),
        ("Devpost", None),
        ("nill", None),
        ("", None),
    ],
)
def test_real_organiser_names(organiser: str, expected: str | None) -> None:
    assert match(organiser) == expected


def test_short_names_only_match_exactly() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    orgs = [(a, "ITC"), (b, "Example University")]
    assert best_match("ITC", orgs) == a
    assert best_match("Hackitc Club ITC Chapter", orgs) is None  # too short to contain
    assert best_match("CS Dept, Example University", orgs) == b


def test_longest_contained_name_wins() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    orgs = [(a, "Anna University"), (b, "Anna University Regional Campus Madurai")]
    assert best_match("Anna University Regional Campus Madurai, TN", orgs) == b


def test_norm() -> None:
    assert norm("I.I.T. (Main), Delhi") == "i i t delhi"
    assert norm("NIT Trichy") == "national institute of technology trichy"
