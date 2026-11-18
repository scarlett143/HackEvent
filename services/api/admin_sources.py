"""Admin organisations + sources (HE-091, doc 06 A7, App Flow F10).

Every route is gated by the router-level `require_admin_2fa`; admins see every row
(doc 07 §3), so there is no per-user ownership filter here.

- `GET /admin/organisations` (filters kind, tier, region, q), `PATCH /admin/organisations/{id}`
  (tier, is_active), `POST /admin/organisations/import` (CSV text, validated by the seed
  loader's `parse_csv`, all-or-nothing: any bad row → 422 listing every problem).
- `GET/POST /admin/sources`, `GET/PATCH/DELETE /admin/sources/{id}`. URLs pass the SSRF
  guard (doc 07 §8) on create and on every URL change; create/edit/delete write audit rows.
- `GET /admin/sources/health` (HE-092): every source with `common.source_health` status
  (red / amber / green) and its reason, red first; same rule the scheduler alerts on.
- `POST /admin/sources/{id}/run` enqueues one `scrape_source` job (the same payload the
  scheduler sends, own job id) and returns 202; the result lands on the source row
  (last_success_at / error_streak / last_error), which the UI polls via GET.

`last_error` and `selector_config` are returned as JSON data; the UI renders them as text.
"""

import json
import uuid
from datetime import datetime
from typing import Annotated, Any

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import Auth
from api.deps import get_db, get_redis
from api.errors import ApiError
from api.invites import _ip
from api.totp import require_admin_2fa
from common.audit import AuditAction, record
from common.fetch import ssrf
from common.models import (
    Organisation,
    OrgKind,
    OrgRegion,
    SocialNetwork,
    Source,
    SourceAdapter,
    SourceKind,
)
from common.queues import SCRAPE_QUEUE
from common.source_health import HealthStatus, source_health
from seeds.load import SeedError, parse_csv, upsert
from workers.scheduler import cadence_hours_for, scrape_payload
from workers.scrape.adapters import ADAPTERS

router = APIRouter(prefix="/api/v1/admin", dependencies=[Depends(require_admin_2fa)])

ORG_MAX_ROWS = 1000  # ponytail: ~600 seeded orgs fit one page; add the doc 06 cursor past that
SOURCE_MAX_ROWS = 1000
CSV_MAX_CHARS = 1_000_000
SELECTOR_CONFIG_MAX_BYTES = 16_384
API_PARSERS = sorted(set(ADAPTERS) - {"rss"})  # `api` sources name one in selector_config
RUN_LIMIT = 10  # doc 06 A8: /admin/sources/:id/run 10 / hour (per source)
RUN_WINDOW_SECONDS = 3600


# --- organisations ---


class OrgOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: OrgKind
    region: OrgRegion
    tier: int
    rank_source: str
    website: str
    is_active: bool
    source_count: int


class OrgList(BaseModel):
    items: list[OrgOut]
    next_cursor: None = None


class OrgPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tier: int | None = Field(None, ge=1, le=3)
    is_active: bool | None = None


class CsvIn(BaseModel):
    csv: str = Field(max_length=CSV_MAX_CHARS)


class ImportOut(BaseModel):
    imported: int


async def _org_out(db: AsyncSession, org: Organisation) -> OrgOut:
    n = await db.scalar(select(func.count()).where(Source.organisation_id == org.id))
    return OrgOut(
        id=org.id,
        name=org.name,
        kind=org.kind,
        region=org.region,
        tier=org.tier,
        rank_source=org.rank_source,
        website=org.website,
        is_active=org.is_active,
        source_count=n or 0,
    )


async def _recadence(db: AsyncSession, org_ids: list[uuid.UUID]) -> None:
    """The org tier changed: re-derive cadence for its sources that follow the org tier."""
    rows = await db.execute(
        select(Source, Organisation.tier)
        .join(Organisation, Organisation.id == Source.organisation_id)
        .where(Source.organisation_id.in_(org_ids), Source.tier_override.is_(None))
    )
    for source, tier in rows:
        source.cadence_hours = cadence_hours_for(source.kind.value, tier, None)


@router.get("/organisations")
async def list_organisations(
    db: Annotated[AsyncSession, Depends(get_db)],
    kind: OrgKind | None = None,
    tier: Annotated[int | None, Query(ge=1, le=3)] = None,
    region: OrgRegion | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> OrgList:  # role: admin + 2FA (router dependency); admins see every org
    counts = (
        select(Source.organisation_id, func.count().label("n"))
        .group_by(Source.organisation_id)
        .subquery()
    )
    stmt = select(Organisation, func.coalesce(counts.c.n, 0)).outerjoin(
        counts, counts.c.organisation_id == Organisation.id
    )
    if kind:
        stmt = stmt.where(Organisation.kind == kind)
    if tier:
        stmt = stmt.where(Organisation.tier == tier)
    if region:
        stmt = stmt.where(Organisation.region == region)
    if q and q.strip():
        stmt = stmt.where(Organisation.name.icontains(q.strip(), autoescape=True))
    rows = await db.execute(
        stmt.order_by(Organisation.tier, Organisation.name, Organisation.id).limit(ORG_MAX_ROWS)
    )
    return OrgList(
        items=[
            OrgOut(
                id=o.id,
                name=o.name,
                kind=o.kind,
                region=o.region,
                tier=o.tier,
                rank_source=o.rank_source,
                website=o.website,
                is_active=o.is_active,
                source_count=n,
            )
            for o, n in rows
        ]
    )


@router.patch("/organisations/{org_id}")
async def update_organisation(
    org_id: uuid.UUID,
    body: OrgPatch,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OrgOut:  # role: admin + 2FA. Doc 07 §11 lists no organisation audit action.
    org = await db.scalar(select(Organisation).where(Organisation.id == org_id).with_for_update())
    if org is None:
        raise ApiError(404, "not_found", "Not found.")
    if body.is_active is not None:
        org.is_active = body.is_active
    if body.tier is not None and body.tier != org.tier:
        org.tier = body.tier
        await db.flush()
        await _recadence(db, [org.id])
    await db.commit()
    await db.refresh(org)
    return await _org_out(db, org)


@router.post("/organisations/import")
async def import_organisations(
    body: CsvIn,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ImportOut:  # role: admin + 2FA; same columns and rules as services/seeds/*.csv
    text = body.csv.removeprefix("﻿")  # spreadsheet exports often start with a BOM
    try:
        rows = parse_csv(text.splitlines(keepends=True), "upload")
    except SeedError as e:
        raise ApiError(
            422, "invalid_csv", "Nothing was imported. Fix these rows.", {"errors": e.errors}
        ) from None
    if not rows:
        raise ApiError(
            422, "invalid_csv", "Nothing was imported. The file has no rows.", {"errors": []}
        )
    ids = list((await db.scalars(upsert(rows).returning(Organisation.id))).all())
    await _recadence(db, ids)
    await db.commit()
    return ImportOut(imported=len(rows))


# --- sources ---


class SourceOut(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID | None
    organisation_name: str | None
    kind: SourceKind
    network: SocialNetwork | None
    url: str
    adapter: SourceAdapter
    selector_config: dict[str, Any] | None
    tier_override: int | None
    cadence_hours: int
    is_enabled: bool
    next_run_at: datetime
    last_success_at: datetime | None
    last_event_at: datetime | None
    error_streak: int
    empty_streak: int
    last_error: str | None
    updated_at: datetime


class SourceList(BaseModel):
    items: list[SourceOut]
    next_cursor: None = None


class SourcePatch(BaseModel):
    """Fields left out stay as they are; `null` clears a nullable field."""

    model_config = ConfigDict(extra="forbid")
    organisation_id: uuid.UUID | None = None
    kind: SourceKind | None = None
    network: SocialNetwork | None = None
    url: str | None = Field(None, min_length=1, max_length=2048)
    adapter: SourceAdapter | None = None
    selector_config: dict[str, Any] | None = None
    tier_override: int | None = Field(None, ge=1, le=3)
    cadence_hours: int | None = Field(None, ge=1, le=8760)
    is_enabled: bool | None = None


class SourceIn(SourcePatch):
    kind: SourceKind
    url: str = Field(min_length=1, max_length=2048)
    adapter: SourceAdapter
    is_enabled: bool = True


class RunOut(BaseModel):
    job_id: str


EDITABLE = (
    "organisation_id",
    "kind",
    "network",
    "url",
    "adapter",
    "selector_config",
    "tier_override",
    "cadence_hours",
    "is_enabled",
)
NOT_NULL = {"kind", "url", "adapter", "cadence_hours", "is_enabled"}


def _source_out(s: Source, org_name: str | None) -> SourceOut:
    return SourceOut(
        id=s.id,
        organisation_id=s.organisation_id,
        organisation_name=org_name,
        kind=s.kind,
        network=s.network,
        url=s.url,
        adapter=s.adapter,
        selector_config=s.selector_config,
        tier_override=s.tier_override,
        cadence_hours=s.cadence_hours,
        is_enabled=s.is_enabled,
        next_run_at=s.next_run_at,
        last_success_at=s.last_success_at,
        last_event_at=s.last_event_at,
        error_streak=s.error_streak,
        empty_streak=s.empty_streak,
        last_error=s.last_error,
        updated_at=s.updated_at,
    )


def _bad(message: str) -> ApiError:
    return ApiError(422, "invalid_source", message)


async def _check_url(url: str) -> str:
    url = url.strip()
    try:
        await ssrf.check_url(url)  # doc 07 §8; the fetcher re-checks on every request
    except ssrf.BlockedURL as e:
        raise ApiError(422, "url_blocked", f"This URL can't be used: {e}") from None
    return url


async def _validate(db: AsyncSession, v: dict[str, Any]) -> int | None:
    """Check the combined source fields; return the organisation's tier (None if none)."""
    if (v["kind"] == SourceKind.social) != (v["network"] is not None):
        raise _bad("Social sources need a network; other kinds must not set one.")
    if v["kind"] == SourceKind.aggregator and v["organisation_id"] is not None:
        raise _bad("Aggregator sources are not tied to an organisation.")
    cfg = v["selector_config"]
    if cfg is not None and len(json.dumps(cfg).encode()) > SELECTOR_CONFIG_MAX_BYTES:
        raise _bad("selector_config is too large.")
    if "render" in (cfg or {}) and not isinstance(cfg["render"], bool):
        raise _bad("selector_config.render must be true or false.")
    if v["adapter"] == SourceAdapter.api and (cfg or {}).get("parser") not in API_PARSERS:
        raise _bad(f"API sources need selector_config.parser, one of: {', '.join(API_PARSERS)}.")
    if v["organisation_id"] is None:
        return None
    tier = await db.scalar(select(Organisation.tier).where(Organisation.id == v["organisation_id"]))
    if tier is None:
        raise _bad("Organisation not found.")
    return tier


async def _load_source(db: AsyncSession, source_id: uuid.UUID, *, lock: bool = False) -> Source:
    stmt = select(Source).where(Source.id == source_id)
    source = await db.scalar(stmt.with_for_update() if lock else stmt)
    if source is None:
        raise ApiError(404, "not_found", "Not found.")
    return source


async def _org_name(db: AsyncSession, org_id: uuid.UUID | None) -> str | None:
    if org_id is None:
        return None
    return await db.scalar(select(Organisation.name).where(Organisation.id == org_id))


def _jsonable(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


@router.get("/sources")
async def list_sources(
    db: Annotated[AsyncSession, Depends(get_db)],
    organisation_id: uuid.UUID | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> SourceList:  # role: admin + 2FA; admins see every source
    stmt = select(Source, Organisation.name).outerjoin(
        Organisation, Organisation.id == Source.organisation_id
    )
    if organisation_id:
        stmt = stmt.where(Source.organisation_id == organisation_id)
    if q and q.strip():
        needle = q.strip()
        stmt = stmt.where(
            or_(
                Source.url.icontains(needle, autoescape=True),
                Organisation.name.icontains(needle, autoescape=True),
            )
        )
    rows = await db.execute(stmt.order_by(Source.created_at, Source.id).limit(SOURCE_MAX_ROWS))
    return SourceList(items=[_source_out(s, name) for s, name in rows])


@router.post("/sources", status_code=201)
async def create_source(
    body: SourceIn,
    request: Request,
    auth: Annotated[Auth, Depends(require_admin_2fa)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SourceOut:  # role: admin + 2FA
    values = body.model_dump(include=set(EDITABLE))
    values["url"] = await _check_url(body.url)
    tier = await _validate(db, values)
    if values["cadence_hours"] is None:
        values["cadence_hours"] = cadence_hours_for(body.kind.value, tier, body.tier_override)
    source = Source(**values)
    db.add(source)
    await db.flush()
    await record(
        db,
        action=AuditAction.SOURCE_CREATE,
        actor_id=auth.user.id,
        target_type="source",
        target_id=source.id,
        ip=_ip(request),
        meta={k: _jsonable(v) for k, v in values.items()},
    )
    await db.commit()
    await db.refresh(source)
    return _source_out(source, await _org_name(db, source.organisation_id))


class HealthRow(BaseModel):
    id: uuid.UUID
    url: str
    organisation_name: str | None
    kind: SourceKind
    adapter: SourceAdapter
    is_enabled: bool
    status: HealthStatus
    reason: str
    last_success_at: datetime | None
    last_error: str | None
    error_streak: int
    empty_streak: int


class HealthList(BaseModel):
    items: list[HealthRow]
    next_cursor: None = None


HEALTH_ORDER = {HealthStatus.RED: 0, HealthStatus.AMBER: 1, HealthStatus.GREEN: 2}


# Declared before /sources/{source_id} so "health" is not parsed as an id.
@router.get("/sources/health")
async def sources_health(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HealthList:  # role: admin + 2FA (HE-092); red first, then amber, then green
    rows = await db.execute(
        select(Source, Organisation.name)
        .outerjoin(Organisation, Organisation.id == Source.organisation_id)
        .limit(SOURCE_MAX_ROWS)
    )
    items = []
    for s, name in rows:
        health = source_health(s.error_streak, s.empty_streak, s.last_error)
        items.append(
            HealthRow(
                id=s.id,
                url=s.url,
                organisation_name=name,
                kind=s.kind,
                adapter=s.adapter,
                is_enabled=s.is_enabled,
                status=health.status,
                reason=health.reason,
                last_success_at=s.last_success_at,
                last_error=s.last_error,
                error_streak=s.error_streak,
                empty_streak=s.empty_streak,
            )
        )
    items.sort(key=lambda h: (HEALTH_ORDER[h.status], not h.is_enabled, h.url, str(h.id)))
    return HealthList(items=items)


@router.get("/sources/{source_id}")
async def get_source(
    source_id: uuid.UUID, db: Annotated[AsyncSession, Depends(get_db)]
) -> SourceOut:  # role: admin + 2FA; the "Run now" UI polls this for the result
    source = await _load_source(db, source_id)
    return _source_out(source, await _org_name(db, source.organisation_id))


@router.patch("/sources/{source_id}")
async def update_source(
    source_id: uuid.UUID,
    body: SourcePatch,
    request: Request,
    auth: Annotated[Auth, Depends(require_admin_2fa)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SourceOut:  # role: admin + 2FA
    source = await _load_source(db, source_id, lock=True)
    given = body.model_dump(include=body.model_fields_set & set(EDITABLE))
    if nulls := sorted(k for k in NOT_NULL if k in given and given[k] is None):
        raise _bad(f"{', '.join(nulls)} can't be empty.")
    if "url" in given:
        given["url"] = await _check_url(given["url"])
    current = {k: getattr(source, k) for k in EDITABLE}
    values = current | given
    tier = await _validate(db, values)
    tier_inputs = ("kind", "organisation_id", "tier_override")
    if "cadence_hours" not in given and any(values[k] != current[k] for k in tier_inputs):
        values["cadence_hours"] = cadence_hours_for(
            values["kind"].value, tier, values["tier_override"]
        )
    changed = {k: v for k, v in values.items() if v != current[k]}
    if changed:
        if "url" in changed:  # validators belong to the old URL
            source.etag = source.last_modified = source.content_hash = None
        for k, v in changed.items():
            setattr(source, k, v)
        await record(
            db,
            action=AuditAction.SOURCE_EDIT,
            actor_id=auth.user.id,
            target_type="source",
            target_id=source.id,
            ip=_ip(request),
            meta={
                k: {"from": _jsonable(current[k]), "to": _jsonable(v)} for k, v in changed.items()
            },
        )
    await db.commit()
    await db.refresh(source)
    return _source_out(source, await _org_name(db, source.organisation_id))


@router.delete("/sources/{source_id}", status_code=204)
async def delete_source(
    source_id: uuid.UUID,
    request: Request,
    auth: Annotated[Auth, Depends(require_admin_2fa)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:  # role: admin + 2FA
    source = await _load_source(db, source_id, lock=True)
    await record(
        db,
        action=AuditAction.SOURCE_DELETE,
        actor_id=auth.user.id,
        target_type="source",
        target_id=source.id,
        ip=_ip(request),
        meta={"url": source.url, "kind": source.kind.value},
    )
    await db.delete(source)
    try:
        await db.commit()
    except IntegrityError:  # raw_documents keep their source; history is not deleted
        raise ApiError(
            409, "source_in_use", "This source has scraped documents. Disable it instead."
        ) from None
    return Response(status_code=204)


@router.post("/sources/{source_id}/run", status_code=202)
async def run_source(
    source_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> RunOut:  # role: admin + 2FA. Enqueues one scrape; changes nothing on the row.
    source = await _load_source(db, source_id)
    key = f"rl:source_run:{source.id}"
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, RUN_WINDOW_SECONDS, nx=True)
        count = (await pipe.execute())[0]
    if count > RUN_LIMIT:
        retry = max(int(await redis.ttl(key)), 1)
        raise ApiError(
            429,
            "rate_limited",
            "This source has been run 10 times in the last hour. Try again later.",
            headers={"Retry-After": str(retry)},
        )
    job_id = f"run:{source.id}:{uuid.uuid4().hex}"
    await ArqRedis(redis.connection_pool).enqueue_job(
        "scrape_source",
        scrape_payload(
            str(source.id),
            source.url,
            source.etag,
            source.last_modified,
            source.content_hash,
            source.kind.value,
            source.selector_config,
        ),
        _job_id=job_id,
        _queue_name=SCRAPE_QUEUE,
    )
    return RunOut(job_id=job_id)
