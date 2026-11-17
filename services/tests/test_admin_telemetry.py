"""Admin telemetry: LLM spend vs caps + call stats. Needs the test stack; see tests/stack.py."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from tests.stack import make_actor, needs_stack, sql

pytestmark = needs_stack

URL = "/api/v1/admin/telemetry"


@pytest.fixture
def c(stack_client: TestClient) -> Iterator[TestClient]:
    sql("TRUNCATE llm_usage, llm_providers CASCADE")
    yield stack_client


def provider(name: str, priority: int) -> str:
    (pid,) = sql(
        "INSERT INTO llm_providers (name, type, model, api_key_enc, role, priority,"
        " input_price_per_mtok, output_price_per_mtok, daily_cap_usd, monthly_cap_usd, is_enabled)"
        " VALUES (:n, 'gemini', :n, '\\x00', 'extract', :p, 1, 1, 2.00, 20.00, true)"
        " RETURNING id::text",
        n=name,
        p=priority,
    )[0]
    return str(pid)


def usage(pid: str, cost: str, status: str, at: datetime, latency: int) -> None:
    sql(
        "INSERT INTO llm_usage (provider_id, job_id, input_tokens, output_tokens, cost_usd,"
        " cache_hit, ok, at, status, latency_ms)"
        " VALUES (CAST(:p AS uuid), 'j', 60, 40, :c, false, :ok, :at, :s, :l)",
        p=pid,
        c=cost,
        ok=status == "ok",
        at=at,
        s=status,
        l=latency,
    )


def test_refused_for_non_admin_and_anonymous(c: TestClient) -> None:
    assert c.get(URL).status_code == 401
    for role in ("professor", "management"):
        r = c.get(URL, headers=make_actor(f"{role}@example.com", role).headers)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "forbidden"


def test_spend_windows_call_stats_and_idle_provider(c: TestClient) -> None:
    admin = make_actor("admin@example.com", "admin")
    busy = provider("busy", 1)
    provider("idle", 2)
    now = datetime.now(UTC)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today = day + timedelta(seconds=1)
    yesterday = day - timedelta(seconds=1)  # last month when today is the 1st
    in_month = yesterday.month == day.month
    long_ago = day - timedelta(days=40)

    usage(busy, "0.50000", "ok", today, 200)
    usage(busy, "0.25000", "ok", today, 400)
    usage(busy, "0.00000", "rate_limited", today, 50)
    usage(busy, "0.10000", "invalid_output", today, 80)
    usage(busy, "0.40000", "ok", yesterday, 100)  # month window only (when same month)
    usage(busy, "9.00000", "ok", long_ago, 100)  # outside both windows

    body = c.get(URL, headers=admin.headers).json()
    assert [p["name"] for p in body["providers"]] == ["busy", "idle"]  # priority order
    b, i = body["providers"]
    assert float(b["spent_day_usd"]) == pytest.approx(0.85)
    assert float(b["spent_month_usd"]) == pytest.approx(0.85 + (0.40 if in_month else 0))
    assert (b["calls_day"], b["failed_calls_day"], b["tokens_day"]) == (4, 2, 400)
    assert b["avg_latency_ms_day"] == 300  # mean of today's ok calls (200, 400)
    assert (float(b["daily_cap_usd"]), float(b["monthly_cap_usd"])) == (2.0, 20.0)
    assert (float(i["spent_day_usd"]), i["calls_day"], i["avg_latency_ms_day"]) == (0.0, 0, None)
    assert body["total_events"] == sql("SELECT count(*) FROM events")[0][0]
    assert datetime.fromisoformat(body["day_resets_at"]) == day + timedelta(days=1)
