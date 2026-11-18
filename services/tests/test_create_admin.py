"""scripts.create_admin: first Admin only, password policy, no duplicate email."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from tests.stack import DB_URL, needs_stack, sql

from api.auth import _verify
from scripts import create_admin


@pytest.fixture
def empty(stack_client: TestClient) -> None:
    sql("TRUNCATE users CASCADE")


def run(email: str, password: str = "correct horse battery staple") -> None:
    assert DB_URL
    asyncio.run(create_admin.create(DB_URL, email, "First Admin", password))


@needs_stack
def test_creates_the_first_admin_then_refuses(empty: None) -> None:
    run("first@example.com")
    (row,) = sql("SELECT email::text, role::text, is_active, password_hash FROM users")
    assert row[:3] == ("first@example.com", "admin", True)
    assert _verify(row[3], "correct horse battery staple")
    with pytest.raises(SystemExit, match="already exists"):
        run("second@example.com")
    assert len(sql("SELECT 1 FROM users")) == 1


@needs_stack
def test_refuses_an_email_already_in_use(empty: None) -> None:
    sql("INSERT INTO users (email, name, role) VALUES ('prof@example.com', 'P', 'professor')")
    with pytest.raises(SystemExit, match="prof@example.com already exists"):
        run("prof@example.com")


def test_weak_password_is_refused_before_any_db_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["create_admin", "--email", "a@example.com", "--name", "A"])
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "short")
    monkeypatch.setattr(create_admin, "create", None)  # would raise TypeError if reached
    with pytest.raises(SystemExit, match="Weak password"):
        create_admin.main()
