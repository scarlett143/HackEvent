"""Static policy checks on infra/docker-compose.yml (TRD §3, Security doc §7-8)."""

from pathlib import Path
from typing import Any

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "infra" / "docker-compose.yml"
TRD_SERVICES = {
    "caddy",
    "web",
    "api",
    "scheduler",
    "worker-scrape",
    "worker-llm",
    "worker-notify",
    "postgres",
    "redis",
    "ntfy",
}
TRD_MEMORY_MB = {
    "caddy": 128,
    "web": 512,
    "api": 512,
    "scheduler": 256,
    "worker-scrape": 1536,
    "worker-llm": 512,
    "worker-notify": 256,
    "postgres": 1024,
    "redis": 256,
    "ntfy": 128,
}


def _services() -> dict[str, Any]:
    data = yaml.safe_load(COMPOSE.read_text())
    services: dict[str, Any] = data["services"]
    return services


def _mb(value: str) -> int:
    unit = value[-1].lower()
    return int(value[:-1]) * (1024 if unit == "g" else 1)


def test_all_trd_services_present() -> None:
    assert set(_services()) == TRD_SERVICES


@pytest.mark.parametrize("name", sorted(TRD_SERVICES))
def test_service_has_required_policy(name: str) -> None:
    svc = _services()[name]
    assert _mb(svc["mem_limit"]) == TRD_MEMORY_MB[name]
    assert 0 < float(svc["cpus"]) <= 2
    assert svc["restart"] == "unless-stopped"
    assert svc["healthcheck"]["test"]


def test_only_caddy_publishes_ports() -> None:
    published = {n for n, s in _services().items() if s.get("ports")}
    assert published == {"caddy"}


def test_scraper_has_no_db_credentials() -> None:
    env = _services()["worker-scrape"].get("environment", {})
    assert "DATABASE_URL" not in env
    assert "postgres" not in _services()["worker-scrape"].get("depends_on", {})
