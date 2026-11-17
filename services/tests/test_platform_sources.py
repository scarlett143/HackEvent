"""Seeded hackathon-platform sources (seeds/platforms.py)."""

import asyncio
import ipaddress

import pytest
from fastapi.testclient import TestClient
from tests.stack import DB_URL, needs_stack, sql

from common.fetch import ssrf
from common.models import SourceAdapter
from seeds import platforms
from workers.scheduler import cadence_hours_for


def test_platform_list_is_well_formed() -> None:
    urls = [u for u, _, _ in platforms.PLATFORMS]
    assert len(urls) == len(set(urls))
    assert all(u.startswith("https://") for u in urls)
    for url, adapter, config in platforms.PLATFORMS:
        if adapter is SourceAdapter.api:
            assert config and config.get("parser") in {"devpost", "unstop"}, (
                url
            )  # dedicated parsers only
        else:
            assert adapter is SourceAdapter.llm and config is None, url


@needs_stack
def test_load_is_idempotent_and_uses_aggregator_cadence(
    stack_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def resolve(host: str, port: int) -> list[ssrf.IPAddress]:
        return [ipaddress.ip_address("93.184.216.34")]  # public: the guard passes without real DNS

    monkeypatch.setattr(ssrf, "resolve", resolve)
    sql("TRUNCATE sources CASCADE")
    assert asyncio.run(platforms.load(DB_URL or "")) == {
        "added": len(platforms.PLATFORMS),
        "skipped": 0,
    }
    assert asyncio.run(platforms.load(DB_URL or "")) == {
        "added": 0,
        "skipped": len(platforms.PLATFORMS),
    }
    rows = sql("SELECT kind::text, cadence_hours, is_enabled FROM sources")
    assert len(rows) == len(platforms.PLATFORMS)
    assert {r for r in rows} == {("aggregator", cadence_hours_for("aggregator", None, None), True)}


@needs_stack
def test_a_blocked_url_writes_nothing(
    stack_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def resolve(host: str, port: int) -> list[ssrf.IPAddress]:
        return [ipaddress.ip_address("10.0.0.5")]  # private address: the SSRF guard must refuse

    monkeypatch.setattr(ssrf, "resolve", resolve)
    sql("TRUNCATE sources CASCADE")
    with pytest.raises(ssrf.BlockedURL):
        asyncio.run(platforms.load(DB_URL or ""))
    assert sql("SELECT count(*) FROM sources")[0][0] == 0
