"""HE-091: admin organisations + sources, CSV import, Run now. Needs the test stack; see
tests/stack.py. DNS is faked (ssrf.resolve), never the guard itself."""

import ipaddress
import pickle
from collections.abc import Iterator
from typing import Any

import pytest
import redis
from fastapi.testclient import TestClient
from tests.stack import REDIS_URL, Actor, make_actor, needs_stack, sql

from common.fetch import ssrf
from common.queues import SCRAPE_QUEUE

pytestmark = needs_stack

ORGS = "/api/v1/admin/organisations"
SOURCES = "/api/v1/admin/sources"
HEADER = "name,kind,region,tier,rank_source,website\n"
DNS = {"example.org": "93.184.216.34", "internal.example": "10.0.0.5"}


@pytest.fixture
def c(stack_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    async def resolve(host: str, port: int) -> list[ssrf.IPAddress]:
        if host not in DNS:
            raise OSError("no such host")
        return [ipaddress.ip_address(DNS[host])]

    monkeypatch.setattr(ssrf, "resolve", resolve)
    sql("TRUNCATE organisations, sources CASCADE")
    yield stack_client


@pytest.fixture
def admin(c: TestClient) -> Actor:
    return make_actor("admin@example.com", "admin")


def org(name: str = "Acme", kind: str = "company", tier: int = 2, region: str = "india") -> str:
    (oid,) = sql(
        "INSERT INTO organisations (name, kind, region, tier, rank_source, website, is_active)"
        " VALUES (:n, CAST(:k AS org_kind), CAST(:r AS org_region), :t, 'test', '', true)"
        " RETURNING id::text",
        n=name,
        k=kind,
        r=region,
        t=tier,
    )[0]
    return str(oid)


def new_source(c: TestClient, a: Actor, **body: Any) -> Any:
    return c.post(SOURCES, json={"kind": "rss", "adapter": "rss", **body}, headers=a.headers)


def rq() -> Any:  # sync Redis client on the test stack (Any: redis-py's types are a union)
    return redis.Redis.from_url(REDIS_URL or "")


def audit() -> list[tuple[Any, ...]]:
    return sql(
        "SELECT action, actor_id::text, target_id::text, meta FROM audit_log"
        " WHERE action LIKE 'source.%' ORDER BY created_at"
    )


def test_non_admin_and_unverified_admin_are_refused(c: TestClient) -> None:
    oid = org()
    (sid,) = sql(
        "INSERT INTO sources (kind, url, adapter, cadence_hours, is_enabled)"
        " VALUES ('rss', 'https://example.org/feed', 'rss', 72, true) RETURNING id::text"
    )[0]
    calls = [
        ("GET", ORGS, None),
        ("PATCH", f"{ORGS}/{oid}", {"tier": 1}),
        ("POST", f"{ORGS}/import", {"csv": HEADER + "X,company,india,1,t,\n"}),
        ("GET", SOURCES, None),
        ("POST", SOURCES, {"kind": "rss", "adapter": "rss", "url": "https://example.org/a"}),
        ("GET", f"{SOURCES}/{sid}", None),
        ("PATCH", f"{SOURCES}/{sid}", {"is_enabled": False}),
        ("DELETE", f"{SOURCES}/{sid}", None),
        ("POST", f"{SOURCES}/{sid}/run", None),
    ]
    for actor in (make_actor("prof@example.com"), make_actor("mgmt@example.com", "management")):
        for method, path, body in calls:
            r = c.request(method, path, json=body, headers=actor.headers)
            assert r.status_code == 403, (method, path)
            assert r.json()["error"]["code"] == "forbidden"
    admin = make_actor("admin@example.com", "admin")
    sql("UPDATE sessions SET totp_verified_at = NULL WHERE user_id = CAST(:u AS uuid)", u=admin.id)
    for method, path, body in calls:
        r = c.request(method, path, json=body, headers=admin.headers)
        assert r.json()["error"]["code"] == "totp_required", (method, path)
    assert c.get(SOURCES).status_code == 401
    assert sql("SELECT tier FROM organisations") == [(2,)]
    assert sql("SELECT url, is_enabled FROM sources") == [("https://example.org/feed", True)]
    assert audit() == []
    assert rq().zcard(SCRAPE_QUEUE) == 0


def test_list_filter_and_patch_organisations(c: TestClient, admin: Actor) -> None:
    acme = org("Acme", tier=2)
    org("IIT Example", kind="institution", tier=1)
    org("Globex", tier=3, region="global")

    def names(**q: Any) -> list[str]:
        return [o["name"] for o in c.get(ORGS, params=q, headers=admin.headers).json()["items"]]

    assert names() == ["IIT Example", "Acme", "Globex"]  # tier, then name
    assert names(kind="institution") == ["IIT Example"]
    assert names(tier=3) == ["Globex"]
    assert names(region="india") == ["IIT Example", "Acme"]
    assert names(q="glob") == ["Globex"]
    assert names(q="%") == []  # LIKE wildcards are escaped

    r = new_source(
        c,
        admin,
        url="https://example.org/acme",
        organisation_id=acme,
        kind="official",
        adapter="llm",
    )
    assert r.json()["cadence_hours"] == 72  # tier 2
    pinned = new_source(
        c,
        admin,
        url="https://example.org/p",
        organisation_id=acme,
        kind="official",
        adapter="llm",
        tier_override=3,
    ).json()
    r = c.patch(f"{ORGS}/{acme}", json={"tier": 1, "is_active": False}, headers=admin.headers)
    assert r.status_code == 200
    assert r.json() | {"id": None} == {
        "id": None,
        "name": "Acme",
        "kind": "company",
        "region": "india",
        "tier": 1,
        "rank_source": "test",
        "website": "",
        "is_active": False,
        "source_count": 2,
    }
    # Tier change re-derives cadence for sources following the org tier, not pinned ones.
    assert dict(sql("SELECT url, cadence_hours FROM sources")) == {
        "https://example.org/acme": 24,
        pinned["url"]: 168,
    }
    assert c.patch(f"{ORGS}/{acme}", json={"tier": 4}, headers=admin.headers).status_code == 422
    missing = "00000000-0000-0000-0000-000000000000"
    assert c.patch(f"{ORGS}/{missing}", json={"tier": 1}, headers=admin.headers).status_code == 404


def test_csv_import_all_or_nothing(c: TestClient, admin: Actor) -> None:
    org("Acme", tier=2)
    bad = (
        HEADER
        + "Good Co,company,india,1,Fortune India 500 2025 #3,https://good.example.com\n"
        + "Bad Tier,company,india,7,x,\n"
        + "Bad Kind,startup,mars,1,,http://insecure.example.com\n"
        + "Good Co,company,india,1,dup,\n"
    )
    r = c.post(f"{ORGS}/import", json={"csv": bad}, headers=admin.headers)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "invalid_csv"
    assert err["details"]["errors"] == [
        "upload:3: tier '7' must be 1, 2 or 3",
        "upload:4: kind 'startup' not one of ['company', 'institution']",
        "upload:4: region 'mars' not one of ['india', 'global']",
        "upload:4: rank_source empty",
        "upload:4: website 'http://insecure.example.com' must be an https URL (or empty)",
        "upload:5: duplicate name 'Good Co' (first at upload:2)",
    ]
    assert sql("SELECT name FROM organisations") == [("Acme",)]  # nothing written

    r = c.post(f"{ORGS}/import", json={"csv": "name,tier\nX,1\n"}, headers=admin.headers)
    assert r.status_code == 422 and "header" in r.json()["error"]["details"]["errors"][0]
    r = c.post(f"{ORGS}/import", json={"csv": HEADER}, headers=admin.headers)
    assert r.status_code == 422

    good = (
        "﻿"  # spreadsheet BOM
        + HEADER
        + "acme,company,global,1,Fortune Global 500 2026 #9,https://acme.example.com\r\n"
        + "Acme,institution,india,3,NIRF 2025 Engineering band 201-300,\r\n"
    )
    r = c.post(f"{ORGS}/import", json={"csv": good}, headers=admin.headers)
    assert r.status_code == 200 and r.json() == {"imported": 2}
    assert sorted(sql("SELECT name, kind::text, region::text, tier FROM organisations")) == [
        ("Acme", "institution", "india", 3),
        ("acme", "company", "global", 1),  # upserted by (lower(name), kind)
    ]


def test_source_create_validates_and_audits(c: TestClient, admin: Actor) -> None:
    acme = org("Acme", tier=1)
    for url in (
        "http://127.0.0.1/feed",
        "https://internal.example/feed",  # resolves to 10.0.0.5
        "http://169.254.169.254/latest/meta-data",
        "https://example.org:8443/feed",
        "file:///etc/passwd",
        "https://unknown.invalid/",
    ):
        r = new_source(c, admin, url=url)
        assert r.status_code == 422 and r.json()["error"]["code"] == "url_blocked", url
    for body, message in (
        ({"kind": "social"}, "network"),
        ({"kind": "rss", "network": "x"}, "network"),
        ({"kind": "aggregator", "organisation_id": acme}, "Aggregator"),
        ({"adapter": "api"}, "parser"),
        ({"adapter": "api", "selector_config": {"parser": "rss"}}, "parser"),
        ({"adapter": "llm", "selector_config": {"render": "yes"}}, "render"),
        ({"organisation_id": "00000000-0000-0000-0000-000000000000"}, "Organisation"),
    ):
        r = new_source(c, admin, url="https://example.org/feed", **body)
        assert r.status_code == 422, body
        assert message in r.json()["error"]["message"], body
    assert new_source(c, admin, url="https://example.org/x", adapter="ftp").status_code == 422
    assert sql("SELECT count(*) FROM sources") == [(0,)]
    assert audit() == []

    r = new_source(
        c,
        admin,
        url=" https://example.org/hackathons ",
        kind="aggregator",
        adapter="api",
        selector_config={"parser": "devpost"},
    )
    assert r.status_code == 201
    agg = r.json()
    assert agg["url"] == "https://example.org/hackathons"
    assert agg["cadence_hours"] == 4  # aggregator
    assert agg["is_enabled"] is True and agg["organisation_name"] is None
    r = new_source(
        c, admin, url="https://example.org/a", organisation_id=acme, kind="official", adapter="llm"
    )
    assert r.json()["cadence_hours"] == 24 and r.json()["organisation_name"] == "Acme"
    r = new_source(
        c,
        admin,
        url="https://example.org/b",
        organisation_id=acme,
        cadence_hours=6,
        is_enabled=False,
    )
    assert r.json()["cadence_hours"] == 6 and r.json()["is_enabled"] is False
    rows = audit()
    assert [(a, actor) for a, actor, _, _ in rows] == [("source.create", admin.id)] * 3
    assert rows[0][2] == agg["id"]
    assert rows[0][3]["url"] == "https://example.org/hackathons"
    assert rows[0][3]["selector_config"] == {"parser": "devpost"}
    listed = c.get(SOURCES, params={"organisation_id": acme}, headers=admin.headers).json()
    assert [s["url"] for s in listed["items"]] == ["https://example.org/a", "https://example.org/b"]
    assert [
        s["url"]
        for s in c.get(SOURCES, params={"q": "hack"}, headers=admin.headers).json()["items"]
    ] == [agg["url"]]


def test_source_edit_and_delete(c: TestClient, admin: Actor) -> None:
    acme = org("Acme", tier=2)
    s = new_source(c, admin, url="https://example.org/feed", organisation_id=acme).json()
    assert s["cadence_hours"] == 72
    sql("UPDATE sources SET etag = 'e', content_hash = 'h'")
    path = f"{SOURCES}/{s['id']}"

    r = c.patch(path, json={"url": "http://10.1.2.3/"}, headers=admin.headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "url_blocked"
    for body in ({"url": None}, {"kind": None}, {"tier_override": 0}, {"bogus": 1}):
        assert c.patch(path, json=body, headers=admin.headers).status_code == 422, body

    r = c.patch(path, json={"tier_override": 1}, headers=admin.headers)
    assert r.status_code == 200 and r.json()["cadence_hours"] == 24
    assert sql("SELECT etag, content_hash FROM sources") == [("e", "h")]  # same URL
    r = c.patch(
        path, json={"url": "https://example.org/new", "cadence_hours": 12}, headers=admin.headers
    )
    assert r.json()["url"] == "https://example.org/new" and r.json()["cadence_hours"] == 12
    assert sql("SELECT etag, content_hash FROM sources") == [(None, None)]  # new URL, re-fetch
    r = c.patch(path, json={"tier_override": None}, headers=admin.headers)
    assert r.json()["tier_override"] is None and r.json()["cadence_hours"] == 72
    assert (
        c.patch(path, json={"is_enabled": True}, headers=admin.headers).status_code == 200
    )  # no-op
    edits = [m for a, _, t, m in audit() if a == "source.edit" and t == s["id"]]
    assert edits == [
        {"tier_override": {"from": None, "to": 1}, "cadence_hours": {"from": 72, "to": 24}},
        {
            "url": {"from": "https://example.org/feed", "to": "https://example.org/new"},
            "cadence_hours": {"from": 24, "to": 12},
        },
        {"tier_override": {"from": 1, "to": None}, "cadence_hours": {"from": 12, "to": 72}},
    ]

    # A source with scraped history can't be deleted (raw_documents keep their source).
    sql(
        "INSERT INTO raw_documents (source_id, url, content_hash, body, status)"
        " VALUES (CAST(:s AS uuid), 'https://example.org/new', 'h', 'b', 'new')",
        s=s["id"],
    )
    r = c.delete(path, headers=admin.headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "source_in_use"
    assert "source.delete" not in [a for a, *_ in audit()]
    sql("DELETE FROM raw_documents")
    assert c.delete(path, headers=admin.headers).status_code == 204
    assert sql("SELECT count(*) FROM sources") == [(0,)]
    assert audit()[-1][:3] == ("source.delete", admin.id, s["id"])
    assert c.get(path, headers=admin.headers).status_code == 404
    assert c.patch(path, json={"is_enabled": False}, headers=admin.headers).status_code == 404
    assert c.delete(path, headers=admin.headers).status_code == 404
    assert c.post(f"{path}/run", headers=admin.headers).status_code == 404


def test_run_now_enqueues_exactly_one_scrape(c: TestClient, admin: Actor) -> None:
    s = new_source(c, admin, url="https://example.org/feed", kind="social", network="x").json()
    other = new_source(c, admin, url="https://example.org/other").json()
    sql("UPDATE sources SET etag = 'e', content_hash = 'h' WHERE id = CAST(:s AS uuid)", s=s["id"])
    before = sql("SELECT * FROM sources ORDER BY id")

    r = c.post(f"{SOURCES}/{s['id']}/run", headers=admin.headers)
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    assert job_id.startswith(f"run:{s['id']}:")
    rd = rq()
    assert [j.decode() for j in rd.zrange(SCRAPE_QUEUE, 0, -1)] == [job_id]
    raw = rd.get(f"arq:job:{job_id}")
    assert raw is not None
    assert sql("SELECT * FROM sources ORDER BY id") == before  # Run now sets nothing
    assert other["id"] not in str(rd.zrange(SCRAPE_QUEUE, 0, -1))

    r2 = c.post(f"{SOURCES}/{s['id']}/run", headers=admin.headers)
    assert r2.json()["job_id"] != job_id  # distinct id per click, never deduped away
    assert rd.zcard(SCRAPE_QUEUE) == 2


def test_run_now_payload_matches_scheduler(c: TestClient, admin: Actor) -> None:
    s = new_source(c, admin, url="https://example.org/feed", kind="social", network="x").json()
    sql("UPDATE sources SET etag = 'e', content_hash = 'h'")
    job_id = c.post(f"{SOURCES}/{s['id']}/run", headers=admin.headers).json()["job_id"]
    raw = rq().get(f"arq:job:{job_id}")
    job = pickle.loads(raw or b"")
    assert job["f"] == "scrape_source"
    assert job["a"] == (
        {
            "id": s["id"],
            "url": "https://example.org/feed",
            "etag": "e",
            "last_modified": None,
            "content_hash": "h",
            "respect_robots": False,  # social
            "render": False,  # HE-027: no selector_config.render
        },
    )


def test_run_now_rate_limited_per_source(c: TestClient, admin: Actor) -> None:
    s = new_source(c, admin, url="https://example.org/feed").json()
    other = new_source(c, admin, url="https://example.org/other").json()
    for _ in range(10):
        assert c.post(f"{SOURCES}/{s['id']}/run", headers=admin.headers).status_code == 202
    r = c.post(f"{SOURCES}/{s['id']}/run", headers=admin.headers)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    assert c.post(f"{SOURCES}/{other['id']}/run", headers=admin.headers).status_code == 202
    assert rq().zcard(SCRAPE_QUEUE) == 11


def test_last_error_is_returned_as_plain_data(c: TestClient, admin: Actor) -> None:
    s = new_source(c, admin, url="https://example.org/feed").json()
    html = "<script>alert(1)</script> HTTP 500"
    sql("UPDATE sources SET error_streak = 2, last_error = :e", e=html)
    r = c.get(f"{SOURCES}/{s['id']}", headers=admin.headers)
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["last_error"] == html and r.json()["error_streak"] == 2


# --- HE-092: source health ---

HEALTH = f"{SOURCES}/health"


def test_health_status_reason_and_order(c: TestClient, admin: Actor) -> None:
    acme = org("Acme")
    for name, errors, empties, last_error, enabled in (
        ("green", 0, 0, None, True),
        ("amber-err", 1, 0, "HTTP 503", True),
        ("red-empty", 0, 5, None, True),
        ("amber-empty", 0, 2, None, True),
        ("red-err", 3, 0, "<b>FetchError</b>: timed out", True),
        ("red-off", 4, 0, None, False),
    ):
        sid = new_source(
            c, admin, url=f"https://example.org/{name}", organisation_id=acme, is_enabled=enabled
        ).json()["id"]
        sql(
            "UPDATE sources SET error_streak = :e, empty_streak = :m, last_error = :l"
            " WHERE id = CAST(:i AS uuid)",
            e=errors,
            m=empties,
            l=last_error,
            i=sid,
        )
    r = c.get(HEALTH, headers=admin.headers)
    assert r.status_code == 200
    items = r.json()["items"]
    assert [(i["url"].rsplit("/", 1)[1], i["status"], i["reason"]) for i in items] == [
        ("red-empty", "red", "5 run(s) in a row with no events (red at 5)"),
        (
            "red-err",
            "red",
            "3 failed run(s) in a row (red at 3); last error: <b>FetchError</b>: timed out",
        ),
        ("red-off", "red", "4 failed run(s) in a row (red at 3)"),  # disabled: last within red
        ("amber-empty", "amber", "2 run(s) in a row with no events (red at 5)"),
        ("amber-err", "amber", "1 failed run(s) in a row (red at 3); last error: HTTP 503"),
        ("green", "green", "OK"),
    ]
    first = items[0]
    assert first["organisation_name"] == "Acme" and first["last_success_at"] is None
    assert set(first) == {
        "id", "url", "organisation_name", "kind", "adapter", "is_enabled", "status",
        "reason", "last_success_at", "last_error", "error_streak", "empty_streak",
    }  # fmt: skip
    assert items[2]["is_enabled"] is False


def test_health_refused_to_non_admins(c: TestClient) -> None:
    for actor in (make_actor("prof@example.com"), make_actor("mgmt@example.com", "management")):
        r = c.get(HEALTH, headers=actor.headers)
        assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    admin = make_actor("admin@example.com", "admin")
    sql("UPDATE sessions SET totp_verified_at = NULL WHERE user_id = CAST(:u AS uuid)", u=admin.id)
    assert c.get(HEALTH, headers=admin.headers).json()["error"]["code"] == "totp_required"
    assert c.get(HEALTH).status_code == 401
