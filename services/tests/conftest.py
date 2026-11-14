"""Shared fixtures for real Postgres + Redis tests; see tests/stack.py for usage."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from tests.stack import Pair, make_actor, open_stack

from api.main import app as main_app


@pytest.fixture
def stack_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    yield from open_stack(monkeypatch, main_app)


@pytest.fixture
def two_users(stack_client: TestClient) -> Pair:
    """Two signed-in professors, A and B, for cross-user tests."""
    return make_actor("a@example.com"), make_actor("b@example.com")
