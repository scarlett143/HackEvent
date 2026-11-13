"""Baseline schema (doc 05) and Alembic migration checks (HE-004).

Static tests run everywhere. The round-trip tests need a real, THROWAWAY Postgres 16 and
are skipped unless TEST_DATABASE_URL is set. They run `downgrade base`, so every table in
that database is dropped. Example:

    docker run -d --rm --name he004-pg -e POSTGRES_PASSWORD=test \
        -e POSTGRES_USER=hackevents -e POSTGRES_DB=hackevents -p 55432:5432 postgres:16-alpine
    TEST_DATABASE_URL=postgresql+asyncpg://hackevents:test@localhost:55432/hackevents \
        uv run pytest services/tests/test_migrations.py
"""

import asyncio
import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import UniqueConstraint, text
from sqlalchemy.ext.asyncio import create_async_engine

import common.models  # noqa: F401  (registers every table on Base.metadata)
from common.db import Base

SERVICES = Path(__file__).resolve().parents[1]
ALEMBIC_INI = SERVICES / "alembic.ini"

DOC05_TABLES = {
    "users",
    "sessions",
    "invites",
    "organisations",
    "sources",
    "raw_documents",
    "events",
    "event_sources",
    "registrations",
    "tracks",
    "calendar_items",
    "calendar_feeds",
    "notification_prefs",
    "channel_endpoints",
    "reminders",
    "notifications",
    "llm_providers",
    "llm_usage",
    "llm_cache",
    "social_sessions",
    "audit_log",
}
EXTENSIONS = {"pgcrypto", "pg_trgm", "citext"}


def _config(url: str | None = None) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["skip_logging"] = True
    if url:
        cfg.attributes["database_url"] = url
    return cfg


# --- static (no DB) ---


def test_models_cover_doc05_tables() -> None:
    assert set(Base.metadata.tables) == DOC05_TABLES


def test_reminder_unique_key() -> None:
    uniques = [
        tuple(c.name for c in con.columns)
        for con in Base.metadata.tables["reminders"].constraints
        if isinstance(con, UniqueConstraint)
    ]
    assert ("user_id", "event_id", "kind", "offset_days", "channel") in uniques


def test_deadline_and_event_dates_are_separate_utc_columns() -> None:
    events = Base.metadata.tables["events"]
    for col in ("registration_deadline", "event_start", "event_end"):
        assert events.c[col].type.timezone is True  # type: ignore[attr-defined]


def test_all_datetimes_are_timestamptz() -> None:
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if col.type.__class__.__name__ == "DateTime":
                assert col.type.timezone is True, f"{table.name}.{col.name}"  # type: ignore[attr-defined]


def test_single_baseline_head() -> None:
    script = ScriptDirectory.from_config(_config())
    assert script.get_heads() == ["0001"]
    assert script.get_revision("0001").down_revision is None


def test_image_points_alembic_at_services_config() -> None:
    dockerfile = (SERVICES / "Dockerfile").read_text()
    assert re.search(r"ALEMBIC_CONFIG=/app/services/alembic\.ini", dockerfile)
    assert "COPY services ./services" in dockerfile
    assert "script_location = %(here)s/migrations" in ALEMBIC_INI.read_text()


# --- round-trip against a real Postgres ---

DB_URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")


async def _snapshot(url: str) -> dict[str, set[str]]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            tables = await conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
            enums = await conn.execute(
                text(
                    "SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace"
                    " WHERE n.nspname = 'public' AND t.typtype = 'e'"
                )
            )
            exts = await conn.execute(text("SELECT extname FROM pg_extension"))
            return {
                "tables": {r[0] for r in tables} - {"alembic_version"},
                "enums": {r[0] for r in enums},
                "extensions": {r[0] for r in exts},
            }
    finally:
        await engine.dispose()


def snapshot(url: str) -> dict[str, set[str]]:
    return asyncio.run(_snapshot(url))


@pytest.fixture
def db_url() -> Iterator[str]:
    assert DB_URL
    command.downgrade(_config(DB_URL), "base")
    yield DB_URL
    command.downgrade(_config(DB_URL), "base")


def _assert_empty(url: str) -> None:
    snap = snapshot(url)
    assert snap["tables"] == set()
    assert snap["enums"] == set()
    assert not (EXTENSIONS & snap["extensions"])


@needs_db
def test_upgrade_creates_all_tables_and_downgrade_removes_them(db_url: str) -> None:
    cfg = _config(db_url)
    command.upgrade(cfg, "head")
    snap = snapshot(db_url)
    assert snap["tables"] == DOC05_TABLES
    assert EXTENSIONS <= snap["extensions"]
    assert {"user_role", "channel", "social_network", "reminder_status"} <= snap["enums"]
    command.check(cfg)  # raises if the migration and the models disagree

    command.downgrade(cfg, "base")
    _assert_empty(db_url)


@needs_db
def test_upgrade_downgrade_upgrade_is_repeatable(db_url: str) -> None:
    cfg = _config(db_url)
    for _ in range(2):
        command.upgrade(cfg, "head")
        assert snapshot(db_url)["tables"] == DOC05_TABLES
        command.downgrade(cfg, "base")
        _assert_empty(db_url)
    command.upgrade(cfg, "head")
    assert snapshot(db_url)["tables"] == DOC05_TABLES


@needs_db
def test_reminder_unique_key_enforced(db_url: str) -> None:
    command.upgrade(_config(db_url), "head")

    async def run() -> str:
        engine = create_async_engine(db_url)
        try:
            async with engine.begin() as conn:
                uid = (
                    await conn.execute(
                        text(
                            "INSERT INTO users (email, name, role)"
                            " VALUES ('a@example.com', 'A', 'professor') RETURNING id"
                        )
                    )
                ).scalar_one()
                eid = (
                    await conn.execute(
                        text(
                            "INSERT INTO events (title, organiser_name, description,"
                            " registration_url, mode, status, review_status, confidence,"
                            " dedupe_key) VALUES ('T', 'O', '', 'https://example.com',"
                            " 'online', 'open', 'auto', 0.9, 'k') RETURNING id"
                        )
                    )
                ).scalar_one()
                insert = text(
                    "INSERT INTO reminders (user_id, event_id, kind, offset_days, channel,"
                    " scheduled_at, status) VALUES (:u, :e, 'deadline', 1, 'email', now(),"
                    " 'pending') ON CONFLICT (user_id, event_id, kind, offset_days, channel)"
                    " DO NOTHING"
                )
                await conn.execute(insert, {"u": uid, "e": eid})
                await conn.execute(insert, {"u": uid, "e": eid})
                count = (await conn.execute(text("SELECT count(*) FROM reminders"))).scalar_one()
                return str(count)
        finally:
            await engine.dispose()

    assert asyncio.run(run()) == "1"
