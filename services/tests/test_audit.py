"""Audit log writer (HE-016).

Sanitisation tests run everywhere. The DB test needs a THROWAWAY Postgres 16 (it runs
`downgrade base`, dropping every table) and is skipped unless TEST_DATABASE_URL is set;
see test_migrations.py for the docker command.
"""

import asyncio
import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from common.audit import (
    MAX_META_BYTES,
    MAX_STRING,
    TRUNCATED,
    AuditAction,
    record,
    sanitize_meta,
)
from common.logging import REDACTED
from common.models import AuditLog, User

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLXZhbHVl"
SK = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"
GOOGLE = "AIza" + "B" * 35
SECRETS = (JWT, SK, GOOGLE, "hunter2hunter2", "cookie-value-xyz")

SECRET_META = {
    "provider": "gemini",
    "api_key": GOOGLE,
    "password": "hunter2hunter2",
    "Cookie": "cookie-value-xyz",
    "session_id": "abc",
    "note": f"rotated to {SK}",
    "nested": {"refresh_token": "x", "keep": 1, "deeper": {"auth": f"Bearer {JWT}"}},
    "list": [{"secret": "s"}, JWT],
}


def _assert_no_secret(blob: object) -> None:
    text = json.dumps(blob)
    for s in SECRETS:
        assert s not in text


def test_secret_keys_dropped_and_values_redacted() -> None:
    clean = sanitize_meta(SECRET_META)
    assert set(clean) == {"provider", "note", "nested", "list"}
    assert clean["note"] == f"rotated to {REDACTED}"
    assert clean["nested"] == {"keep": 1, "deeper": {"auth": f"Bearer {REDACTED}"}}
    assert clean["list"] == [{}, REDACTED]
    _assert_no_secret(clean)


def test_non_secret_keys_kept() -> None:
    meta = {"max_tokens": 10, "token_bucket": "b", "old_role": "professor", "monkey": True}
    assert sanitize_meta(meta) == meta


def test_non_json_values_stringified() -> None:
    uid = uuid.uuid4()
    assert sanitize_meta({"id": uid, "t": (1, 2)}) == {"id": str(uid), "t": [1, 2]}


def test_long_string_truncated_after_redaction() -> None:
    clean = sanitize_meta({"s": "a" * (MAX_STRING - 5) + " " + SK})
    assert clean["s"].endswith(TRUNCATED)
    assert len(clean["s"]) == MAX_STRING + len(TRUNCATED)
    assert "sk-" not in clean["s"]


def test_oversized_meta_replaced() -> None:
    meta = {f"k{i}": "v" * 100 for i in range(100)}
    clean = sanitize_meta(meta)
    assert clean["_truncated"] is True
    assert clean["_bytes"] > MAX_META_BYTES


def test_deep_nesting_capped() -> None:
    meta: dict[str, object] = {}
    cur = meta
    for _ in range(10):
        cur["d"] = {}
        cur = cur["d"]  # type: ignore[assignment]
    assert json.dumps(sanitize_meta(meta)).count('"d"') < 10


def test_action_list_matches_doc() -> None:
    assert len(AuditAction) == 17
    assert all("." in a.value for a in AuditAction)


async def test_unknown_action_rejected() -> None:
    with pytest.raises(ValueError):
        await record(
            None,  # type: ignore[arg-type]
            action="auth.sudo",  # type: ignore[arg-type]
            actor_id=None,
            target_type="user",
            target_id=None,
            ip="127.0.0.1",
            meta={},
        )


# --- real Postgres ---

DB_URL = os.environ.get("TEST_DATABASE_URL")


def _config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["skip_logging"] = True
    cfg.attributes["database_url"] = url
    return cfg


@pytest.fixture
def db_url() -> Iterator[str]:
    if not DB_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    command.downgrade(_config(DB_URL), "base")
    command.upgrade(_config(DB_URL), "head")
    yield DB_URL
    command.downgrade(_config(DB_URL), "base")


def test_every_action_recorded_without_secrets(db_url: str) -> None:
    async def run() -> list[AuditLog]:
        engine = create_async_engine(db_url)
        try:
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as session, session.begin():
                user = User(email="admin@example.com", name="A", role="admin")
                session.add(user)
                await session.flush()
                for action in AuditAction:
                    await record(
                        session,
                        action=action,
                        actor_id=user.id,
                        target_type="user",
                        target_id=user.id,
                        ip="203.0.113.7",
                        meta=SECRET_META,
                    )
            async with sessions() as session:
                return list((await session.scalars(select(AuditLog))).all())
        finally:
            await engine.dispose()

    rows = asyncio.run(run())
    assert sorted(r.action for r in rows) == sorted(a.value for a in AuditAction)
    for r in rows:
        assert r.ip == "203.0.113.7"
        assert r.at is not None
        assert r.meta["provider"] == "gemini"
        _assert_no_secret(r.meta)
