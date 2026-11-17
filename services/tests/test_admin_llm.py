"""HE-094: LLM providers admin — write-only encrypted keys. Needs the test stack; see tests/stack.py."""

import base64
import ipaddress
import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.stack import Actor, make_actor, needs_stack, sql

from common import crypto
from common.config import get_settings
from common.fetch import ssrf

pytestmark = needs_stack

URL = "/api/v1/admin/llm-providers"
MASTER = base64.b64encode(b"k" * 32).decode()
KEY = "sk-test-0123456789abcdef"


@pytest.fixture
def c(stack_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("MASTER_KEY", MASTER)
    get_settings.cache_clear()

    async def resolve(host: str, port: int) -> list[ssrf.IPAddress]:
        return [ipaddress.ip_address({"api.example.org": "93.184.216.34"}.get(host, "10.0.0.5"))]

    monkeypatch.setattr(ssrf, "resolve", resolve)
    sql("TRUNCATE llm_usage, llm_providers CASCADE")
    yield stack_client
    get_settings.cache_clear()


@pytest.fixture
def admin(c: TestClient) -> Actor:
    return make_actor("admin@example.com", "admin")


def body(**over: Any) -> dict[str, Any]:
    return {
        "name": "Gemini Flash",
        "type": "gemini",
        "model": "gemini-1.5-flash",
        "role": "extract",
        "priority": 1,
        "input_price_per_mtok": "0.075",
        "output_price_per_mtok": "0.3",
        "daily_cap_usd": "2",
        "monthly_cap_usd": "20",
        "api_key": KEY,
    } | over


def stored(pid: str) -> bytes:
    return bytes(
        sql("SELECT api_key_enc FROM llm_providers WHERE id = CAST(:i AS uuid)", i=pid)[0][0]
    )


def test_only_verified_admins_can_use_it(c: TestClient) -> None:
    assert c.get(URL).status_code == 401
    for role in ("professor", "management"):
        who = make_actor(f"{role}@example.com", role)
        assert c.get(URL, headers=who.headers).status_code == 403
        assert c.post(URL, json=body(), headers=who.headers).status_code == 403
    admin = make_actor("admin@example.com", "admin")
    sql("UPDATE sessions SET totp_verified_at = NULL WHERE user_id = CAST(:u AS uuid)", u=admin.id)
    assert c.get(URL, headers=admin.headers).json()["error"]["code"] == "totp_required"
    assert sql("SELECT count(*) FROM llm_providers")[0][0] == 0


def test_key_is_write_only_and_encrypted_at_rest(c: TestClient, admin: Actor) -> None:
    r = c.post(URL, json=body(), headers=admin.headers)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["has_key"] is True and "api_key" not in out and KEY not in r.text
    listed = c.get(URL, headers=admin.headers)
    assert KEY not in listed.text and listed.json()[0]["name"] == "Gemini Flash"
    blob = stored(out["id"])
    assert KEY.encode() not in blob
    aad = crypto.aad("llm_providers", "api_key_enc", uuid.UUID(out["id"]))
    assert crypto.decrypt(blob, aad, base64.b64decode(MASTER)).decode() == KEY


def test_rotating_the_key_and_audit_rows_never_hold_the_key(c: TestClient, admin: Actor) -> None:
    pid = c.post(URL, json=body(), headers=admin.headers).json()["id"]
    new = "sk-rotated-fedcba9876543210"
    r = c.patch(f"{URL}/{pid}", json={"api_key": new, "priority": 2}, headers=admin.headers)
    assert r.status_code == 200 and new not in r.text and r.json()["priority"] == 2
    aad = crypto.aad("llm_providers", "api_key_enc", uuid.UUID(pid))
    assert crypto.decrypt(stored(pid), aad, base64.b64decode(MASTER)).decode() == new
    assert (
        c.patch(f"{URL}/{pid}", json={"daily_cap_usd": "3"}, headers=admin.headers).status_code
        == 200
    )
    rows = sql(
        "SELECT action, meta FROM audit_log WHERE target_type = 'llm_provider' ORDER BY created_at"
    )
    assert [a for a, _ in rows] == ["provider.add", "provider.edit", "cap.change"]
    assert rows[1][1]["fields"] == ["api_key", "priority"]
    dump = json.dumps([m for _, m in rows])
    assert KEY not in dump and new not in dump


def test_validation(c: TestClient, admin: Actor) -> None:
    def code(**over: Any) -> str:
        return str(c.post(URL, json=body(**over), headers=admin.headers).json()["error"]["code"])

    assert code(monthly_cap_usd="1") == "invalid_caps"
    assert code(type="openai_compatible") == "invalid_base_url"
    assert (
        code(type="openai_compatible", base_url="http://api.example.org/v1") == "invalid_base_url"
    )
    assert code(type="openai_compatible", base_url="https://internal.example/v1") == "url_blocked"
    ok = c.post(
        URL,
        json=body(type="openai_compatible", base_url="https://api.example.org/v1"),
        headers=admin.headers,
    )
    assert ok.status_code == 201 and ok.json()["base_url"] == "https://api.example.org/v1"
    assert c.post(URL, json=body(api_key="short"), headers=admin.headers).status_code == 422
    assert (
        c.patch(f"{URL}/{uuid.uuid4()}", json={"priority": 3}, headers=admin.headers).status_code
        == 404
    )


def test_missing_master_key_is_a_clear_error_not_a_plaintext_save(
    c: TestClient, admin: Actor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MASTER_KEY")
    get_settings.cache_clear()
    r = c.post(URL, json=body(), headers=admin.headers)
    assert r.status_code == 503 and r.json()["error"]["code"] == "master_key_missing"
    assert sql("SELECT count(*) FROM llm_providers")[0][0] == 0
