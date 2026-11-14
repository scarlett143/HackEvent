"""`call_llm()`: the single LLM choke point (HE-043). Routing, cost caps, fallback, usage.

AGENTS.md: never bypass the cost-cap check, so nothing else may call
`LLMProvider.complete_json`. Per call:

1. Candidates = enabled `llm_providers` rows with the requested role, then rows with role
   `fallback`; each group by `priority` ascending (lower = tried first), then id.
2. Skip a provider cooling down after a 429 (Redis key, TTL = its retry_after).
3. Reserve, fail-closed and race-safe: in one short transaction lock the provider row
   (`SELECT ... FOR UPDATE`, so concurrent reservations for one provider serialise across
   processes), sum this UTC day's and UTC month's `llm_usage.cost_usd` (in-flight
   reservations included), and only if `spent + estimate <= cap` for both windows insert a
   `reserved` usage row holding the estimate. Otherwise no call is made. The estimate is
   an upper bound: input = UTF-8 bytes of system + user + schema (a BPE token is >= 1
   byte) + overhead, output = max_output_tokens. Price <= 0 counts as unknown: skipped.
4. Call, then settle the row: success -> actual tokens and cost; 429 -> cost 0 (not
   billed) + cooldown; InvalidOutput / other ProviderError -> keep the estimate (the
   provider may have billed, and we can't tell). A crash mid-call leaves the reservation
   in place, which errs on the side of the cap.
5. InvalidOutput retries the same provider once (doc 08 A5), then moves on. Any other
   failure moves to the next provider. None left -> `NoProviderAvailable`; the caller
   marks the work for retry / pauses the queue (doc 08 A7).

Windows are UTC (doc 08: store times in UTC; caps don't name a timezone).
"""

import json
import logging
import math
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from typing import Any

import httpx2
from redis.asyncio import Redis
from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from common.crypto import CryptoError
from common.llm.base import InvalidOutput, LLMResult, ProviderError, RateLimited
from common.llm.factory import build_provider
from common.models import LlmProvider, LlmProviderRole, LlmUsage

log = logging.getLogger(__name__)

INPUT_OVERHEAD_TOKENS = 256  # chat template / structured-output framing the provider adds
DEFAULT_COOLDOWN_S = 60.0
MAX_COOLDOWN_S = 3600.0
INVALID_OUTPUT_TRIES = 2  # doc 08 A5: retry once, then fallback provider
_MTOK = Decimal(1_000_000)
_COST_QUANTUM = Decimal("0.00001")  # llm_usage.cost_usd is numeric(10,5)


class NoProviderAvailable(Exception):
    """Every candidate is disabled, over cap, cooling down, unpriced or failed. No prompt text."""


def cooldown_key(provider_id: uuid.UUID) -> str:
    return f"he:llm:cooldown:{provider_id}"


def estimate_input_tokens(system: str, user: str, schema: dict[str, Any]) -> int:
    text = system + user + json.dumps(schema)
    return len(text.encode()) + INPUT_OVERHEAD_TOKENS


def cost_usd(
    input_tokens: int, output_tokens: int, input_price: Decimal, output_price: Decimal
) -> Decimal:
    """Price columns are USD per 1M tokens. Rounded up to numeric(10,5)."""
    raw = (input_tokens * input_price + output_tokens * output_price) / _MTOK
    return raw.quantize(_COST_QUANTUM, rounding=ROUND_CEILING)


def _utcnow() -> datetime:  # tests patch this to cross day/month boundaries
    return datetime.now(UTC)


def notify_admin_cap_hit(
    provider: LlmProvider, window: str, spent: Decimal, estimate: Decimal, cap: Decimal
) -> None:
    """Admin alert hook: a provider hit its daily/monthly cap. Log only until the admin
    channel exists (later ticket); grep `llm.cap_hit`."""
    log.warning(
        "llm.cap_hit provider=%s id=%s window=%s spent_usd=%s estimate_usd=%s cap_usd=%s",
        provider.name,
        provider.id,
        window,
        spent,
        estimate,
        cap,
    )


@dataclass(frozen=True)
class _Reservation:
    usage_id: uuid.UUID
    input_price: Decimal
    output_price: Decimal


async def _reserve(
    engine: AsyncEngine,
    provider_id: uuid.UUID,
    *,
    est_input: int,
    max_output: int,
    purpose: str,
    job_id: str,
) -> _Reservation | None:
    now = _utcnow()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month = day.replace(day=1)
    async with AsyncSession(engine) as db, db.begin():
        row = await db.scalar(
            select(LlmProvider).where(LlmProvider.id == provider_id).with_for_update()
        )
        if row is None or not row.is_enabled:
            return None
        pin, pout = row.input_price_per_mtok, row.output_price_per_mtok
        if pin <= 0 or pout <= 0:
            log.warning("llm.price_unknown provider=%s id=%s", row.name, row.id)
            return None
        estimate = cost_usd(est_input, max_output, pin, pout)
        spent_day, spent_month = (
            await db.execute(
                select(
                    func.coalesce(
                        func.sum(case((LlmUsage.at >= day, LlmUsage.cost_usd), else_=0)), 0
                    ),
                    func.coalesce(func.sum(LlmUsage.cost_usd), 0),
                ).where(LlmUsage.provider_id == provider_id, LlmUsage.at >= month)
            )
        ).one()
        for window, spent, cap in (
            ("day", spent_day, row.daily_cap_usd),
            ("month", spent_month, row.monthly_cap_usd),
        ):
            if spent + estimate > cap:
                notify_admin_cap_hit(row, window, spent, estimate, cap)
                return None
        usage = LlmUsage(
            provider_id=provider_id,
            job_id=job_id,
            input_tokens=0,
            output_tokens=0,
            cost_usd=estimate,
            cache_hit=False,
            ok=False,
            at=now,
            purpose=purpose,
            model=row.model,
            status="reserved",
        )
        db.add(usage)
        await db.flush()
        return _Reservation(usage.id, pin, pout)


async def _settle(engine: AsyncEngine, usage_id: uuid.UUID, **values: Any) -> None:
    async with AsyncSession(engine) as db, db.begin():
        await db.execute(update(LlmUsage).where(LlmUsage.id == usage_id).values(**values))


async def call_llm(
    engine: AsyncEngine,
    redis: Redis,
    *,
    role: LlmProviderRole,
    system: str,
    user: str,
    schema: dict[str, Any],
    max_output_tokens: int,
    purpose: str,
    job_id: str = "",
    client: httpx2.AsyncClient | None = None,
    master_key: bytes | None = None,
) -> LLMResult:
    """Run one structured-JSON LLM call through routing + caps + fallback (module doc).

    `engine`: usage rows are written in their own short transactions, so they persist even
    if the caller's transaction rolls back. `purpose` and `job_id` go to `llm_usage`;
    prompts, page text and keys never do. `client` / `master_key` are passed to
    `build_provider` (tests inject a MockTransport client). Raises `NoProviderAvailable`.
    """
    async with AsyncSession(engine) as db:
        rows = (
            await db.scalars(
                select(LlmProvider)
                .where(
                    LlmProvider.is_enabled,
                    LlmProvider.role.in_([role, LlmProviderRole.fallback]),
                )
                .order_by((LlmProvider.role != role).asc(), LlmProvider.priority, LlmProvider.id)
            )
        ).all()
    est_input = estimate_input_tokens(system, user, schema)
    for row in rows:
        if await redis.exists(cooldown_key(row.id)):
            log.info("llm.cooling_down provider=%s id=%s", row.name, row.id)
            continue
        try:
            provider = build_provider(row, client=client, master_key=master_key)
        except (ProviderError, CryptoError) as exc:
            log.error("llm.provider_unusable provider=%s id=%s error=%s", row.name, row.id, exc)
            continue
        for _ in range(INVALID_OUTPUT_TRIES):
            res = await _reserve(
                engine,
                row.id,
                est_input=est_input,
                max_output=max_output_tokens,
                purpose=purpose,
                job_id=job_id,
            )
            if res is None:
                break
            start = time.monotonic()
            try:
                result = await provider.complete_json(
                    system=system, user=user, schema=schema, max_output_tokens=max_output_tokens
                )
            except ProviderError as exc:
                latency = int((time.monotonic() - start) * 1000)
                if isinstance(exc, RateLimited):
                    ttl = min(MAX_COOLDOWN_S, max(1.0, exc.retry_after or DEFAULT_COOLDOWN_S))
                    await redis.set(cooldown_key(row.id), "1", ex=math.ceil(ttl))
                    await _settle(
                        engine, res.usage_id, status="rate_limited", cost_usd=0, latency_ms=latency
                    )
                else:
                    status = "invalid_output" if isinstance(exc, InvalidOutput) else "error"
                    await _settle(engine, res.usage_id, status=status, latency_ms=latency)
                log.warning("llm.attempt_failed provider=%s id=%s error=%s", row.name, row.id, exc)
                if isinstance(exc, InvalidOutput):
                    continue
                break
            await _settle(
                engine,
                res.usage_id,
                status="ok",
                ok=True,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cost_usd=cost_usd(
                    result.input_tokens, result.output_tokens, res.input_price, res.output_price
                ),
                model=result.model,
                latency_ms=result.latency_ms,
            )
            return result
    log.error(
        "llm.no_provider_available role=%s purpose=%s candidates=%d", role, purpose, len(rows)
    )
    raise NoProviderAvailable(f"no LLM provider available for role={role}")
