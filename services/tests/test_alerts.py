"""HE-131: ops alerts to Admin (email job + in-app row), debounced in Redis."""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from arq.connections import ArqRedis, RedisSettings, create_pool
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from tests.stack import DB_URL, REDIS_URL, make_actor, needs_stack, sql

from common.email import render
from common.queues import ALL_QUEUES, NOTIFY_QUEUE, SCHEDULER_HEARTBEAT_KEY, health_key
from scripts import send_test_alert
from workers import alerts


def test_ops_alert_template_renders_and_escapes() -> None:
    mail = render("ops_alert", app_domain="he.example.test", problem="Disk <b>", detail="<x>")
    assert mail.subject == "HackEvents ops alert: Disk <b>"
    assert "&lt;x&gt;" in mail.html and "<x>" not in mail.html
    assert "https://he.example.test/api/v1/health" in mail.text


@pytest.fixture
async def ctx(stack_client: TestClient) -> AsyncIterator[dict[str, Any]]:
    assert DB_URL and REDIS_URL
    await asyncio.to_thread(sql, "TRUNCATE notifications, reminders CASCADE")
    await asyncio.to_thread(make_actor, "admin@example.com", "admin")
    await asyncio.to_thread(make_actor, "prof@example.com")
    redis: ArqRedis = await create_pool(RedisSettings.from_dsn(REDIS_URL))
    await redis.set(SCHEDULER_HEARTBEAT_KEY, str(int(time.time())))
    for q in ALL_QUEUES:
        await redis.set(health_key(q), "ok")
    c: dict[str, Any] = {"redis": redis, "db": create_async_engine(DB_URL)}
    yield c
    await c["db"].dispose()
    await redis.aclose()


async def _emails(redis: ArqRedis) -> list[Any]:
    jobs = await redis.queued_jobs(queue_name=NOTIFY_QUEUE)
    return [j.args for j in jobs if j.function == "send_email"]


async def _in_app() -> list[Any]:
    return await asyncio.to_thread(
        sql,
        "SELECT u.email, n.type, n.title, n.body FROM notifications n"
        " JOIN users u ON u.id = n.user_id ORDER BY n.created_at",
    )


@needs_stack
async def test_healthy_stack_sends_nothing(
    ctx: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(alerts, "disk_used_fraction", lambda: 0.5)
    for _ in range(3):
        assert await alerts.check_ops(ctx) == []
    assert await _emails(ctx["redis"]) == [] and await _in_app() == []


@needs_stack
async def test_worker_down_alerts_once_after_two_runs_and_rearms(
    ctx: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(alerts, "disk_used_fraction", lambda: 0.5)
    redis = ctx["redis"]
    await redis.delete(health_key("he:llm"))
    assert await alerts.check_ops(ctx) == []  # one miss (e.g. a restart): no page yet
    assert await alerts.check_ops(ctx) == ["workers"]
    assert await alerts.check_ops(ctx) == []  # still down: not re-sent
    ((to, template, context),) = await _emails(redis)
    assert (to, template) == ("admin@example.com", "ops_alert")  # admins only
    assert context == {"problem": "Worker down", "detail": "No health check from: he:llm."}
    assert await _in_app() == [
        ("admin@example.com", "ops_alert", "Ops alert: Worker down", context["detail"])
    ]

    await redis.set(health_key("he:llm"), "ok")
    assert await alerts.check_ops(ctx) == []  # recovered: re-armed
    await redis.delete(health_key("he:llm"))
    await alerts.check_ops(ctx)
    assert await alerts.check_ops(ctx) == ["workers"]
    assert len(await _emails(redis)) == 2


@needs_stack
async def test_stale_heartbeat_disk_and_reminder_lag(
    ctx: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(alerts, "disk_used_fraction", lambda: 0.91)
    await ctx["redis"].set(SCHEDULER_HEARTBEAT_KEY, str(int(time.time()) - 600))
    async with ctx["db"].connect() as conn:
        found = await alerts.problems(conn, ctx["redis"])
    assert found["scheduler"] is not None and "600 s ago" in found["scheduler"][1]
    assert found["disk"] == ("Disk almost full", "Disk 91% used (threshold 80%).")
    assert found["workers"] is None and found["reminder_lag"] is None
    await ctx["redis"].delete(SCHEDULER_HEARTBEAT_KEY)
    async with ctx["db"].connect() as conn:
        found = await alerts.problems(conn, ctx["redis"])
    assert found["scheduler"] == ("Scheduler stalled", "Last scheduler heartbeat: never.")


def test_disk_used_fraction_is_a_fraction() -> None:
    assert 0 < alerts.disk_used_fraction() < 1


@needs_stack
async def test_send_test_alert_script_reaches_admins(ctx: dict[str, Any]) -> None:
    assert await send_test_alert._send() == 1
    ((to, template, context),) = await _emails(ctx["redis"])
    assert (to, template, context["problem"]) == ("admin@example.com", "ops_alert", "Test alert")
    assert [r[:3] for r in await _in_app()] == [
        ("admin@example.com", "ops_alert", "Ops alert: Test alert")
    ]
