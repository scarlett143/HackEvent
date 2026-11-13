from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api import health
from api.main import app


def _fixed(value: bool) -> Callable[[Any], Coroutine[Any, Any, bool]]:
    async def check(deps: Any) -> bool:
        return value

    return check


@pytest.fixture
def client() -> TestClient:
    app.state.deps = object()  # checks are patched; lifespan not run
    return TestClient(app)


def test_live(client: TestClient) -> None:
    r = client.get("/api/v1/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_ready_all_ok(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("check_db", "check_redis", "check_workers"):
        monkeypatch.setattr(health, name, _fixed(True))
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json() == {"db": "ok", "redis": "ok", "workers": "ok"}


@pytest.mark.parametrize("down", ["db", "redis", "workers"])
def test_ready_reports_down(client: TestClient, monkeypatch: pytest.MonkeyPatch, down: str) -> None:
    for name in ("db", "redis", "workers"):
        monkeypatch.setattr(health, f"check_{name}", _fixed(name != down))
    r = client.get("/api/v1/health")
    assert r.status_code == 503
    assert r.json()[down] == "down"
    assert sum(v == "ok" for v in r.json().values()) == 2


class _FakeRedis:
    def __init__(self, existing: set[str] | None = None, fail: bool = False) -> None:
        self.existing = existing or set()
        self.fail = fail

    async def exists(self, *keys: str) -> int:
        if self.fail:
            raise ConnectionError("redis down")
        return sum(k in self.existing for k in keys)

    async def ping(self) -> bool:
        if self.fail:
            raise ConnectionError("redis down")
        return True


async def test_check_workers_requires_every_queue() -> None:
    from common.queues import ALL_QUEUES, health_key

    all_keys = {health_key(q) for q in ALL_QUEUES}
    ok = health.Deps(engine=None, redis=_FakeRedis(all_keys))  # type: ignore[arg-type]
    missing = health.Deps(engine=None, redis=_FakeRedis(all_keys - {health_key(ALL_QUEUES[0])}))  # type: ignore[arg-type]
    assert await health.check_workers(ok) is True
    assert await health.check_workers(missing) is False


async def test_checks_fail_closed_on_redis_error() -> None:
    deps = health.Deps(engine=None, redis=_FakeRedis(fail=True))  # type: ignore[arg-type]
    assert await health.check_redis(deps) is False
    assert await health.check_workers(deps) is False
