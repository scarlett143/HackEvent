"""Settings API (HE-077, doc 06 A2/A5/A8, App Flow F8, design brief 4.5).

- `PATCH /me`: name, timezone (IANA, checked against zoneinfo), quiet hours ("HH:MM" start
  and end, both or neither; may cross midnight, e.g. 22:00-07:00).
- `GET/PUT /me/notification-prefs`: deadline/event offsets (0-30 days, deduped, sorted
  descending) and per-channel on/off. No row yet -> the documented defaults
  (PRD: 3, 2, 1, 0 days; email + in-app on). Also new-event alerts (HE-078): on/off and
  filters `{region, kind, mode}` (lists; empty = any); omitted in a PUT = unchanged.
- `POST /me/password`: current + new; new must pass `check_password_policy`. All OTHER
  sessions are revoked (doc 07 §4), the caller's is kept. Wrong current password -> 400 and
  counts against the per-account login limit, so a stolen session can't brute-force it.
- `GET /me/export` (HE-123): JSON download of the caller's own data; secrets dropped.
- `POST /channels/{channel}/test`: email (queues the `test` template to the user's own
  address) and in_app (one notifications row). 5 / hour per user (doc 06 A8). Telegram, ntfy
  and push join here with their tickets (HE-074..076).

Every route acts on the caller's own rows only; there is no user id in any path or body.
"""

import json
from datetime import UTC, datetime, time
from functools import cache
from typing import Annotated, Any, Literal
from zoneinfo import available_timezones

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from redis.asyncio import Redis
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import (
    SESSION_COOKIE,
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
from api.csrf import clear_csrf_cookie
from api.deps import get_db, get_redis
from api.errors import ApiError
from api.invites import _ip
from common import reminders
from common.audit import AuditAction, record
from common.models import (
    AuditLog,
    CalendarFeed,
    CalendarItem,
    ChannelEndpoint,
    Event,
    EventMode,
    Invite,
    Notification,
    NotificationPrefs,
    OrgKind,
    OrgRegion,
    Registration,
    Reminder,
    Session,
    Track,
    User,
    UserRole,
)
from common.notifiers import Endpoint, InAppNotifier
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


class NewEventFilters(BaseModel):
    """Same values as the feed's region / kind / mode filters (common.new_event_alerts)."""

    model_config = ConfigDict(extra="forbid")
    region: list[OrgRegion] = Field(default_factory=list, max_length=len(OrgRegion))
    kind: list[OrgKind] = Field(default_factory=list, max_length=len(OrgKind))
    mode: list[EventMode] = Field(default_factory=list, max_length=len(EventMode))


class PrefsIn(BaseModel):
    deadline_offsets_days: list[int] = Field(max_length=MAX_OFFSET_DAYS + 1)
    event_offsets_days: list[int] = Field(max_length=MAX_OFFSET_DAYS + 1)
    channels: dict[ChannelName, bool]
    new_event_alerts: bool | None = None  # None (omitted) = keep
    new_event_filters: NewEventFilters | None = None

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
                "new_event_alerts": True,
                "new_event_filters": {},
            }
        )
    return PrefsOut.model_validate(
        {
            "deadline_offsets_days": row.deadline_offsets_days,
            "event_offsets_days": row.event_offsets_days,
            "channels": DEFAULT_CHANNELS | row.channels,
            "new_event_alerts": row.new_event_alerts,
            "new_event_filters": row.new_event_filters or {},
        }
    )


@router.get("/me/notification-prefs")
async def get_prefs(user: Me, db: Db) -> PrefsOut:  # role: any, own row only
    row = await db.scalar(select(NotificationPrefs).where(NotificationPrefs.user_id == user.id))
    return _prefs_out(row)


@router.put("/me/notification-prefs")
async def put_prefs(body: PrefsIn, user: Me, db: Db) -> PrefsOut:  # role: any, own row only
    values: dict[str, Any] = {
        "deadline_offsets_days": body.deadline_offsets_days,
        "event_offsets_days": body.event_offsets_days,
        "channels": dict(body.channels),
    }
    if body.new_event_alerts is not None:
        values["new_event_alerts"] = body.new_event_alerts
    if body.new_event_filters is not None:
        values["new_event_filters"] = body.new_event_filters.model_dump(mode="json")
    row = await db.scalar(
        insert(NotificationPrefs)
        .values(user_id=user.id, **({"new_event_filters": {}} | values))
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


# --- delete own account ---

DELETE_PHRASE = "delete my account"


class DeleteIn(BaseModel):
    password: str = Field(min_length=1, max_length=1024)
    confirm: str = Field(max_length=100)  # must be the phrase, typed by the user


@router.post("/me/delete", status_code=204)
async def delete_account(
    body: DeleteIn,
    request: Request,
    response: Response,
    auth: Annotated[Auth, Depends(current_auth)],
    db: Db,
    redis: Rds,
) -> None:  # role: any; deletes only the caller's own account
    """Doc 06 A2 / doc 07: removes the user and everything that is theirs (sessions, prefs,
    endpoints, registrations, tracks, reminders, notifications, calendar items cascade;
    invites they sent are deleted); audit entries stay with actor_id set to NULL."""
    user = auth.user
    if " ".join(body.confirm.split()).lower() != DELETE_PHRASE:
        raise ApiError(422, "confirm_phrase", f'Type "{DELETE_PHRASE}" to confirm.')
    keys = _login_keys(user.email)
    await _consume_attempt(redis, keys)  # same 5 / 15 min limit as login: no password guessing
    if user.password_hash is None or not _verify(user.password_hash, body.password):
        raise ApiError(400, "invalid_password", "Password is incorrect.")
    await _refund_attempt(redis, keys)
    if user.role == UserRole.admin:
        admins = await db.scalar(
            select(func.count())
            .select_from(User)
            .where(User.role == UserRole.admin, User.is_active)
        )
        if (admins or 0) <= 1:
            raise ApiError(
                409, "last_admin", "You are the only admin. Make someone else admin first."
            )
    await record(
        db,
        action=AuditAction.ACCOUNT_DELETE,
        actor_id=user.id,
        target_type="user",
        target_id=user.id,
        ip=_ip(request),
        meta={"role": user.role.value},
    )
    await db.execute(delete(Invite).where(Invite.created_by == user.id))
    await db.delete(user)
    await db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    clear_csrf_cookie(response)


# --- data export (HE-123, doc 06 A2, doc 07 DPDP note) ---

# Everything that is the caller's: their user row and every table keyed by user_id, plus
# invites they sent and audit entries they made. Secrets never leave: hashes, encrypted
# columns (TOTP secret, channel addresses) and the TOTP replay counter are dropped. Audit
# `meta` is dropped too, since it can name other people (an invite's email).
_EXPORT_OWNED: tuple[tuple[str, Any], ...] = (
    ("sessions", Session),
    ("notification_prefs", NotificationPrefs),
    ("channel_endpoints", ChannelEndpoint),
    ("registrations", Registration),
    ("tracks", Track),
    ("calendar_items", CalendarItem),
    ("calendar_feeds", CalendarFeed),
    ("reminders", Reminder),
    ("notifications", Notification),
)
_EXPORT_DROP = {"password_hash", "token_hash", "totp_last_step", "meta"}


def _export_row(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in row.__table__.columns:
        if col.key in _EXPORT_DROP or col.key.endswith("_enc"):
            continue
        value = getattr(row, col.key)
        out[col.key] = value.isoformat() if hasattr(value, "isoformat") else value
    return out


@router.get("/me/export")
async def export_me(user: Me, db: Db) -> Response:  # role: any, own rows only
    data: dict[str, Any] = {
        "exported_at": datetime.now(UTC).isoformat(),
        "user": _export_row(user),
    }
    for name, model in _EXPORT_OWNED:
        rows = await db.scalars(select(model).where(model.user_id == user.id))
        data[name] = [_export_row(r) for r in rows]
    invites = await db.scalars(select(Invite).where(Invite.created_by == user.id))
    data["invites_sent"] = [_export_row(r) for r in invites]
    audit = await db.scalars(
        select(AuditLog).where(AuditLog.actor_id == user.id).order_by(AuditLog.at)
    )
    data["audit_log"] = [_export_row(r) for r in audit]
    event_ids = {
        r["event_id"] for k in ("registrations", "tracks", "reminders", "notifications")
        for r in data[k] if r.get("event_id")
    }  # fmt: skip
    titles = await db.execute(select(Event.id, Event.title).where(Event.id.in_(event_ids)))
    data["events"] = {str(i): t for i, t in titles}
    day = datetime.now(UTC).strftime("%Y%m%d")
    return Response(
        content=json.dumps(data, default=str, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="hackevents-export-{day}.json"',
            "Cache-Control": "no-store",
        },
    )


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
    await InAppNotifier(await db.connection()).test(Endpoint(user.id))  # HE-073 plugin
    await db.commit()
    return TestOut(channel=channel, status="delivered")
