"""Settings API (HE-077, doc 06 A2/A5/A8, App Flow F8, design brief 4.5).

- `PATCH /me`: name, timezone (IANA, checked against zoneinfo), quiet hours ("HH:MM" start
  and end, both or neither; may cross midnight, e.g. 22:00-07:00).
- `GET/PUT /me/notification-prefs`: deadline/event offsets (0-30 days, deduped, sorted
  descending) and per-channel on/off. No row yet -> the documented defaults
  (PRD: 3, 2, 1, 0 days; email + in-app on).
- `POST /me/password`: current + new; new must pass `check_password_policy`. All OTHER
  sessions are revoked (doc 07 §4), the caller's is kept. Wrong current password -> 400 and
  counts against the per-account login limit, so a stolen session can't brute-force it.
- `POST /channels/{channel}/test`: email (queues the `test` template to the user's own
  address) and in_app (one notifications row). 5 / hour per user (doc 06 A8). Telegram, ntfy
  and push join here with their tickets (HE-074..076).

Every route acts on the caller's own rows only; there is no user id in any path or body.
"""

from datetime import time
from functools import cache
from typing import Annotated, Literal
from zoneinfo import available_timezones

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator, model_validator
from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import (
    Auth,
    MeOut,
    _consume_attempt,
    _login_keys,
    _me,
    _refund_attempt,
    _verify,
    check_password_policy,
    current_auth,
    current_user,
    hash_password,
)
from api.deps import get_db, get_redis
from api.errors import ApiError
from common import reminders
from common.models import Notification, NotificationPrefs, Session, User
from common.queues import NOTIFY_QUEUE

router = APIRouter(prefix="/api/v1")

Me = Annotated[User, Depends(current_user)]
Db = Annotated[AsyncSession, Depends(get_db)]
Rds = Annotated[Redis, Depends(get_redis)]

MAX_OFFSET_DAYS = 30
DEFAULT_OFFSETS = reminders.DEFAULT_OFFSETS  # PRD §"Reminders": 3, 2, 1, 0 days by default
DEFAULT_CHANNELS = reminders.DEFAULT_CHANNELS
TEST_LIMIT = 5  # doc 06 A8: /channels/:id/test 5 / hour per user
TEST_WINDOW_SECONDS = 3600

ChannelName = Literal["in_app", "email", "fcm", "telegram", "ntfy", "gcal"]
TestableChannel = Literal["email", "in_app"]
HHMM = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


@cache
def _zones() -> frozenset[str]:
    return frozenset(available_timezones())


def _parse_hhmm(value: str | None) -> time | None:
    return None if value is None else time.fromisoformat(value)


async def _after_prefs_change(db: AsyncSession, user: User) -> None:
    """Offsets, channels, timezone and quiet hours all feed the planner (HE-070)."""
    await db.flush()
    await reminders.plan(db, user.id)


# --- profile ---


class MePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    timezone: str | None = Field(default=None, max_length=64)
    quiet_start: Annotated[str, HHMM] | None = None
    quiet_end: Annotated[str, HHMM] | None = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("name must not be blank")
        return v.strip() if v is not None else v

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, v: str | None) -> str | None:
        if v is not None and v not in _zones():
            raise ValueError("unknown timezone")
        return v

    @model_validator(mode="after")
    def _quiet_pair(self) -> "MePatch":
        fields = self.model_fields_set
        if ("quiet_start" in fields) != ("quiet_end" in fields):
            raise ValueError("send quiet_start and quiet_end together")
        if (self.quiet_start is None) != (self.quiet_end is None):
            raise ValueError("quiet hours need both start and end, or neither (off)")
        if self.quiet_start is not None and self.quiet_start == self.quiet_end:
            raise ValueError("quiet hours start and end must differ")
        return self


@router.patch("/me")
async def update_me(body: MePatch, user: Me, db: Db) -> MeOut:  # role: any, own row only
    fields = body.model_fields_set
    if "name" in fields and body.name is not None:
        user.name = body.name
    if "timezone" in fields and body.timezone is not None:
        user.timezone = body.timezone
    if "quiet_start" in fields:
        user.quiet_start = _parse_hhmm(body.quiet_start)
        user.quiet_end = _parse_hhmm(body.quiet_end)
    db.add(user)
    await _after_prefs_change(db, user)
    await db.commit()
    return _me(user)


# --- notification prefs ---


class PrefsIn(BaseModel):
    deadline_offsets_days: list[int] = Field(max_length=MAX_OFFSET_DAYS + 1)
    event_offsets_days: list[int] = Field(max_length=MAX_OFFSET_DAYS + 1)
    channels: dict[ChannelName, bool]

    @field_validator("deadline_offsets_days", "event_offsets_days")
    @classmethod
    def _offsets(cls, v: list[int]) -> list[int]:
        if any(not 0 <= d <= MAX_OFFSET_DAYS for d in v):
            raise ValueError(f"offsets must be 0-{MAX_OFFSET_DAYS} days")
        return sorted(set(v), reverse=True)


class PrefsOut(PrefsIn):
    pass


def _prefs_out(row: NotificationPrefs | None) -> PrefsOut:
    if row is None:
        return PrefsOut.model_validate(
            {
                "deadline_offsets_days": DEFAULT_OFFSETS,
                "event_offsets_days": DEFAULT_OFFSETS,
                "channels": DEFAULT_CHANNELS,
            }
        )
    return PrefsOut.model_validate(
        {
            "deadline_offsets_days": row.deadline_offsets_days,
            "event_offsets_days": row.event_offsets_days,
            "channels": DEFAULT_CHANNELS | row.channels,
        }
    )


@router.get("/me/notification-prefs")
async def get_prefs(user: Me, db: Db) -> PrefsOut:  # role: any, own row only
    row = await db.scalar(select(NotificationPrefs).where(NotificationPrefs.user_id == user.id))
    return _prefs_out(row)


@router.put("/me/notification-prefs")
async def put_prefs(body: PrefsIn, user: Me, db: Db) -> PrefsOut:  # role: any, own row only
    values = {
        "deadline_offsets_days": body.deadline_offsets_days,
        "event_offsets_days": body.event_offsets_days,
        "channels": dict(body.channels),
    }
    row = await db.scalar(
        insert(NotificationPrefs)
        .values(user_id=user.id, new_event_filters={}, **values)
        .on_conflict_do_update(index_elements=["user_id"], set_=values)
        .returning(NotificationPrefs)
    )
    await _after_prefs_change(db, user)
    await db.commit()
    return _prefs_out(row)


# --- password ---


class PasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)


@router.post("/me/password", status_code=204)
async def change_password(
    body: PasswordIn, auth: Annotated[Auth, Depends(current_auth)], db: Db, redis: Rds
) -> None:  # role: any; own password + own other sessions only
    user = auth.user
    keys = _login_keys(user.email)
    await _consume_attempt(redis, keys)
    if user.password_hash is None or not _verify(user.password_hash, body.current_password):
        raise ApiError(400, "invalid_password", "Current password is incorrect.")
    await _refund_attempt(redis, keys)
    check_password_policy(body.new_password)
    user.password_hash = hash_password(body.new_password)
    db.add(user)
    await db.execute(
        delete(Session).where(Session.user_id == user.id, Session.id != auth.session.id)
    )
    await db.commit()


# --- channel tests ---


class TestOut(BaseModel):
    channel: TestableChannel
    status: Literal["queued", "delivered"]


async def _test_rate_limit(redis: Redis, user: User) -> None:
    key = f"rl:channel_test:{user.id}"
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, TEST_WINDOW_SECONDS, nx=True)
        count = (await pipe.execute())[0]
    if count > TEST_LIMIT:
        retry = max(int(await redis.ttl(key)), 1)
        raise ApiError(
            429,
            "rate_limited",
            "Too many test messages. Try again later.",
            headers={"Retry-After": str(retry)},
        )


@router.post("/channels/{channel}/test", status_code=202)
async def test_channel(
    channel: TestableChannel, user: Me, db: Db, redis: Rds
) -> TestOut:  # role: any; sends only to the caller's own address / inbox
    await _test_rate_limit(redis, user)
    if channel == "email":
        await ArqRedis(redis.connection_pool).enqueue_job(
            "send_email", user.email, "test", {}, _queue_name=NOTIFY_QUEUE
        )
        return TestOut(channel=channel, status="queued")
    db.add(
        Notification(
            user_id=user.id,
            type="test",
            title="Test notification",
            body="In-app notifications work.",
        )
    )
    await db.commit()
    return TestOut(channel=channel, status="delivered")
