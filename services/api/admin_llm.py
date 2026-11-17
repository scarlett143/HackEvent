"""LLM providers admin (HE-094): the Stitch "AI Model API Keys & Credential Vault".

Admin + 2FA only (router dependency). Keys are WRITE-ONLY: they are encrypted with the master key
(AES-GCM envelope, AAD bound to llm_providers.api_key_enc + the row id) and never returned or
logged; responses only say `has_key`. Every create/edit writes an audit row (field names only,
never key material). `base_url` (OpenAI-compatible providers) passes the SSRF guard because the
LLM worker will call it.

- `GET /admin/llm-providers` -> providers in routing order (priority, id).
- `POST /admin/llm-providers` -> 201, new provider (api_key required).
- `PATCH /admin/llm-providers/{id}` -> any subset; `api_key` replaces the stored key.

Not here: "test connection" — it costs real LLM calls and must go through common.llm.router's
cost-cap check (AGENTS.md), so it is a separate follow-up.
"""

import uuid
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import Auth
from api.deps import get_db
from api.errors import ApiError
from api.invites import _ip
from api.totp import require_admin_2fa
from common import crypto
from common.audit import AuditAction, record
from common.fetch import ssrf
from common.models import LlmProvider, LlmProviderRole, LlmProviderType

router = APIRouter(prefix="/api/v1/admin", dependencies=[Depends(require_admin_2fa)])

Db = Annotated[AsyncSession, Depends(get_db)]
Admin = Annotated[Auth, Depends(require_admin_2fa)]
Money = Annotated[Decimal, Field(ge=0, le=100_000, max_digits=12, decimal_places=4)]
CAP_FIELDS = {"daily_cap_usd", "monthly_cap_usd"}


class ProviderOut(BaseModel):
    id: uuid.UUID
    name: str
    type: LlmProviderType
    base_url: str | None
    model: str
    role: LlmProviderRole
    priority: int
    input_price_per_mtok: Decimal
    output_price_per_mtok: Decimal
    daily_cap_usd: Decimal
    monthly_cap_usd: Decimal
    is_enabled: bool
    has_key: bool  # the key itself is never returned


class ProviderPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, min_length=1, max_length=80)
    type: LlmProviderType | None = None
    base_url: str | None = Field(None, max_length=2048)
    model: str | None = Field(None, min_length=1, max_length=120)
    role: LlmProviderRole | None = None
    priority: int | None = Field(None, ge=1, le=99)
    input_price_per_mtok: Money | None = None
    output_price_per_mtok: Money | None = None
    daily_cap_usd: Money | None = None
    monthly_cap_usd: Money | None = None
    is_enabled: bool | None = None
    api_key: SecretStr | None = Field(None, min_length=8, max_length=512)


class ProviderIn(ProviderPatch):
    name: str = Field(min_length=1, max_length=80)
    type: LlmProviderType
    model: str = Field(min_length=1, max_length=120)
    role: LlmProviderRole
    priority: int = Field(ge=1, le=99)
    input_price_per_mtok: Money
    output_price_per_mtok: Money
    daily_cap_usd: Money
    monthly_cap_usd: Money
    is_enabled: bool = True
    api_key: SecretStr = Field(min_length=8, max_length=512)


def _out(p: LlmProvider) -> ProviderOut:
    return ProviderOut(
        id=p.id,
        name=p.name,
        type=p.type,
        base_url=p.base_url,
        model=p.model,
        role=p.role,
        priority=p.priority,
        input_price_per_mtok=p.input_price_per_mtok,
        output_price_per_mtok=p.output_price_per_mtok,
        daily_cap_usd=p.daily_cap_usd,
        monthly_cap_usd=p.monthly_cap_usd,
        is_enabled=p.is_enabled,
        has_key=bool(p.api_key_enc),
    )


def _encrypt(key: SecretStr, provider_id: uuid.UUID) -> bytes:
    try:
        return crypto.encrypt(
            key.get_secret_value().strip().encode(),
            crypto.aad("llm_providers", "api_key_enc", provider_id),
        )
    except crypto.CryptoError:
        raise ApiError(
            503, "master_key_missing", "Server key storage is not configured (MASTER_KEY)."
        ) from None


async def _check(
    type_: LlmProviderType, base_url: str | None, daily: Decimal, monthly: Decimal
) -> str | None:
    if monthly < daily:
        raise ApiError(422, "invalid_caps", "The monthly cap can't be lower than the daily cap.")
    if type_ != LlmProviderType.openai_compatible:
        return None  # Gemini / Anthropic use their fixed official endpoints
    url = (base_url or "").strip()
    if not url.startswith("https://"):
        raise ApiError(
            422, "invalid_base_url", "OpenAI-compatible providers need an https:// base URL."
        )
    try:
        await ssrf.check_url(url)
    except ssrf.BlockedURL as e:
        raise ApiError(422, "url_blocked", f"This URL can't be used: {e}") from None
    return url


@router.get("/llm-providers")
async def list_providers(db: Db) -> list[ProviderOut]:  # role: admin + 2FA
    rows = await db.scalars(select(LlmProvider).order_by(LlmProvider.priority, LlmProvider.id))
    return [_out(p) for p in rows]


@router.post("/llm-providers", status_code=201)
async def create_provider(body: ProviderIn, request: Request, auth: Admin, db: Db) -> ProviderOut:
    base_url = await _check(body.type, body.base_url, body.daily_cap_usd, body.monthly_cap_usd)
    pid = uuid.uuid4()
    p = LlmProvider(
        id=pid,
        name=body.name.strip(),
        type=body.type,
        base_url=base_url,
        model=body.model.strip(),
        api_key_enc=_encrypt(body.api_key, pid),
        role=body.role,
        priority=body.priority,
        input_price_per_mtok=body.input_price_per_mtok,
        output_price_per_mtok=body.output_price_per_mtok,
        daily_cap_usd=body.daily_cap_usd,
        monthly_cap_usd=body.monthly_cap_usd,
        is_enabled=body.is_enabled,
    )
    db.add(p)
    await db.flush()
    await record(
        db,
        action=AuditAction.PROVIDER_ADD,
        actor_id=auth.user.id,
        target_type="llm_provider",
        target_id=pid,
        ip=_ip(request),
        meta={"name": p.name, "type": p.type.value, "model": p.model, "role": p.role.value},
    )
    await db.commit()
    return _out(p)


@router.patch("/llm-providers/{provider_id}")
async def edit_provider(
    provider_id: uuid.UUID, body: ProviderPatch, request: Request, auth: Admin, db: Db
) -> ProviderOut:
    p = await db.get(LlmProvider, provider_id)
    if p is None:
        raise ApiError(404, "not_found", "Provider not found.")
    changes: dict[str, Any] = body.model_dump(exclude_unset=True)
    for f in (
        "name",
        "type",
        "model",
        "role",
        "priority",
        "is_enabled",
        *CAP_FIELDS,
        "input_price_per_mtok",
        "output_price_per_mtok",
    ):
        if changes.get(f, ...) is None:
            raise ApiError(422, "invalid_value", f"{f} can't be empty.")
    type_ = changes.get("type", p.type)
    base_url = await _check(
        type_,
        changes.get("base_url", p.base_url),
        changes.get("daily_cap_usd", p.daily_cap_usd),
        changes.get("monthly_cap_usd", p.monthly_cap_usd),
    )
    key = changes.pop("api_key", None)
    for f, v in changes.items():
        setattr(p, f, v.strip() if isinstance(v, str) else v)
    p.base_url = base_url
    if key is not None:
        p.api_key_enc = _encrypt(body.api_key, p.id)  # type: ignore[arg-type]
    fields = sorted([*changes, *(["api_key"] if key is not None else [])])
    await record(
        db,
        action=AuditAction.CAP_CHANGE if CAP_FIELDS & set(changes) else AuditAction.PROVIDER_EDIT,
        actor_id=auth.user.id,
        target_type="llm_provider",
        target_id=p.id,
        ip=_ip(request),
        meta={"fields": fields},  # names only; the key value never reaches the audit log
    )
    await db.commit()
    return _out(p)
