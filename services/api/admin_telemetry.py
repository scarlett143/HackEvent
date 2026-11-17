"""Admin telemetry (Stitch "LLM Quotas & Scraper Telemetry" panel).

`GET /api/v1/admin/telemetry`: per-LLM-provider spend against its daily/monthly caps, call
stats for the current UTC day, and the total event count. Read-only, admin + 2FA at router
level; admins see everything (doc 07 §3), so there is no per-user ownership filter.

Spend uses the same UTC windows and the same rows as the cost-cap check in
`common.llm.router._reserve` (in-flight `reserved` rows included), so the bar an admin sees is
the number the router enforces. Per-source scrape stats already live in `/admin/sources/health`.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.totp import require_admin_2fa
from common.models import Event, LlmProvider, LlmProviderRole, LlmUsage

router = APIRouter(prefix="/api/v1/admin", dependencies=[Depends(require_admin_2fa)])

SETTLED = ("ok", "error", "invalid_output", "rate_limited")  # `reserved` = still in flight


class ProviderStats(BaseModel):
    id: uuid.UUID
    name: str
    model: str
    role: LlmProviderRole
    is_enabled: bool
    daily_cap_usd: Decimal
    monthly_cap_usd: Decimal
    spent_day_usd: Decimal
    spent_month_usd: Decimal
    tokens_day: int
    calls_day: int
    failed_calls_day: int
    avg_latency_ms_day: int | None


class Telemetry(BaseModel):
    day_resets_at: datetime  # next UTC midnight: when the daily caps reset
    providers: list[ProviderStats]
    total_events: int


@router.get("/telemetry")
async def telemetry(db: Annotated[AsyncSession, Depends(get_db)]) -> Telemetry:
    now = datetime.now(UTC)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month = day.replace(day=1)
    today = LlmUsage.at >= day
    settled = LlmUsage.status.in_(SETTLED)

    usage = (
        select(
            LlmUsage.provider_id.label("pid"),
            func.coalesce(func.sum(case((today, LlmUsage.cost_usd), else_=0)), 0).label("day"),
            func.coalesce(func.sum(LlmUsage.cost_usd), 0).label("month"),
            func.coalesce(
                func.sum(case((today, LlmUsage.input_tokens + LlmUsage.output_tokens), else_=0)), 0
            ).label("tokens"),
            func.count(case((today & settled, 1))).label("calls"),
            func.count(case((today & settled & (LlmUsage.status != "ok"), 1))).label("failed"),
            func.avg(case((today & (LlmUsage.status == "ok"), LlmUsage.latency_ms))).label("lat"),
        )
        .where(LlmUsage.at >= month)
        .group_by(LlmUsage.provider_id)
        .subquery()
    )
    rows = await db.execute(
        select(LlmProvider, usage)
        .outerjoin(usage, usage.c.pid == LlmProvider.id)
        .order_by(LlmProvider.priority, LlmProvider.id)
    )
    providers = []
    for p, _pid, spent_day, spent_month, tokens, calls, failed, latency in rows:
        providers.append(
            ProviderStats(
                id=p.id,
                name=p.name,
                model=p.model,
                role=p.role,
                is_enabled=p.is_enabled,
                daily_cap_usd=p.daily_cap_usd,
                monthly_cap_usd=p.monthly_cap_usd,
                spent_day_usd=spent_day or Decimal(0),
                spent_month_usd=spent_month or Decimal(0),
                tokens_day=int(tokens or 0),
                calls_day=calls or 0,
                failed_calls_day=failed or 0,
                avg_latency_ms_day=None if latency is None else round(latency),
            )
        )
    total = await db.scalar(select(func.count()).select_from(Event))
    return Telemetry(
        day_resets_at=day + timedelta(days=1), providers=providers, total_events=total or 0
    )
