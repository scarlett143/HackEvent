"""ORM models for every table in docs/05-Backend-Schema.md §2.

Nullability follows the doc's convention: a column is nullable only when the doc marks it
`null`; everything else is NOT NULL. All times are timestamptz (UTC). Registration deadline
and event dates are separate columns by design.
"""

import enum
import uuid
from datetime import datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from common.db import Base, IdMixin, TimestampMixin, utc_now_column


def _pg_enum(cls: type[enum.Enum], name: str) -> Enum:
    return Enum(cls, name=name, values_callable=lambda e: [m.value for m in e])


def _fk(target: str, ondelete: str | None = None) -> ForeignKey:
    return ForeignKey(target, ondelete=ondelete)


# --- enum types (one Postgres type each; shared where the doc says "as above") ---


class UserRole(enum.StrEnum):
    admin = "admin"
    management = "management"
    professor = "professor"


class OrgKind(enum.StrEnum):
    company = "company"
    institution = "institution"


class OrgRegion(enum.StrEnum):
    india = "india"
    global_ = "global"


class SourceKind(enum.StrEnum):
    official = "official"
    aggregator = "aggregator"
    rss = "rss"
    reddit = "reddit"
    substack = "substack"
    social = "social"


class SocialNetwork(enum.StrEnum):
    linkedin = "linkedin"
    instagram = "instagram"
    x = "x"


class SourceAdapter(enum.StrEnum):
    rss = "rss"
    api = "api"
    selector = "selector"
    llm = "llm"


class RawDocumentStatus(enum.StrEnum):
    new = "new"
    parsed = "parsed"
    failed = "failed"
    skipped = "skipped"


class EventMode(enum.StrEnum):
    online = "online"
    offline = "offline"
    hybrid = "hybrid"
    unknown = "unknown"


class EventStatus(enum.StrEnum):
    open = "open"
    closed = "closed"
    cancelled = "cancelled"


class ReviewStatus(enum.StrEnum):
    auto = "auto"
    needs_review = "needs_review"
    approved = "approved"
    rejected = "rejected"


class Channel(enum.StrEnum):
    fcm = "fcm"
    email = "email"
    telegram = "telegram"
    ntfy = "ntfy"
    gcal = "gcal"


class ChannelPlatform(enum.StrEnum):
    web = "web"
    android = "android"


class ReminderKind(enum.StrEnum):
    deadline = "deadline"
    event = "event"


class ReminderStatus(enum.StrEnum):
    pending = "pending"
    sent = "sent"
    failed = "failed"
    cancelled = "cancelled"


class LlmProviderType(enum.StrEnum):
    gemini = "gemini"
    anthropic = "anthropic"
    openai_compatible = "openai_compatible"


class LlmProviderRole(enum.StrEnum):
    extract = "extract"
    verify = "verify"
    fallback = "fallback"


class SocialSessionStatus(enum.StrEnum):
    valid = "valid"
    expired = "expired"
    banned = "banned"


user_role = _pg_enum(UserRole, "user_role")
channel = _pg_enum(Channel, "channel")
social_network = _pg_enum(SocialNetwork, "social_network")


# --- tables ---


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(CITEXT, unique=True)
    name: Mapped[str] = mapped_column(Text)
    role: Mapped[UserRole] = mapped_column(user_role)
    password_hash: Mapped[str | None] = mapped_column(Text)
    totp_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    google_sub: Mapped[str | None] = mapped_column(Text, unique=True)
    timezone: Mapped[str] = mapped_column(Text, server_default=text("'Asia/Kolkata'"))
    quiet_start: Mapped[time | None] = mapped_column(Time)
    quiet_end: Mapped[time | None] = mapped_column(Time)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class Session(IdMixin, TimestampMixin, Base):
    __tablename__ = "sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID, _fk("users.id", "CASCADE"))
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ip: Mapped[str] = mapped_column(Text)
    user_agent: Mapped[str] = mapped_column(Text)


class Invite(IdMixin, TimestampMixin, Base):
    __tablename__ = "invites"

    email: Mapped[str] = mapped_column(CITEXT)
    role: Mapped[UserRole] = mapped_column(user_role)
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID, _fk("users.id"))


class Organisation(IdMixin, TimestampMixin, Base):
    __tablename__ = "organisations"
    __table_args__ = (
        CheckConstraint("tier BETWEEN 1 AND 3", name="tier_range"),
        Index("uq_organisations_lower_name_kind", func.lower(text("name")), "kind", unique=True),
    )

    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[OrgKind] = mapped_column(_pg_enum(OrgKind, "org_kind"))
    region: Mapped[OrgRegion] = mapped_column(_pg_enum(OrgRegion, "org_region"))
    tier: Mapped[int] = mapped_column(SmallInteger)
    rank_source: Mapped[str] = mapped_column(Text)
    website: Mapped[str] = mapped_column(Text)
    logo_url: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean)


class Source(IdMixin, TimestampMixin, Base):
    __tablename__ = "sources"
    __table_args__ = (Index("ix_sources_next_run_at", "next_run_at"),)

    organisation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, _fk("organisations.id", "SET NULL")
    )
    kind: Mapped[SourceKind] = mapped_column(_pg_enum(SourceKind, "source_kind"))
    network: Mapped[SocialNetwork | None] = mapped_column(social_network)
    url: Mapped[str] = mapped_column(Text)
    adapter: Mapped[SourceAdapter] = mapped_column(_pg_enum(SourceAdapter, "source_adapter"))
    selector_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    tier_override: Mapped[int | None] = mapped_column(SmallInteger)
    cadence_hours: Mapped[int] = mapped_column(Integer)
    next_run_at: Mapped[datetime] = utc_now_column()
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_streak: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    empty_streak: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)
    is_enabled: Mapped[bool] = mapped_column(Boolean)


class RawDocument(IdMixin, TimestampMixin, Base):
    __tablename__ = "raw_documents"
    __table_args__ = (Index("ix_raw_documents_content_hash", "content_hash"),)

    source_id: Mapped[uuid.UUID] = mapped_column(UUID, _fk("sources.id"))
    fetched_at: Mapped[datetime] = utc_now_column()
    url: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[RawDocumentStatus] = mapped_column(
        _pg_enum(RawDocumentStatus, "raw_document_status")
    )


class Event(IdMixin, TimestampMixin, Base):
    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_status_registration_deadline", "status", "registration_deadline"),
        Index("ix_events_first_seen_at_desc", text("first_seen_at DESC")),
        Index(
            "ix_events_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
    )

    title: Mapped[str] = mapped_column(Text)
    organiser_name: Mapped[str] = mapped_column(Text)
    organisation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, _fk("organisations.id", "SET NULL")
    )
    description: Mapped[str] = mapped_column(Text)
    registration_url: Mapped[str] = mapped_column(Text)
    registration_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mode: Mapped[EventMode] = mapped_column(_pg_enum(EventMode, "event_mode"))
    location: Mapped[str | None] = mapped_column(Text)
    eligibility: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    prize: Mapped[str | None] = mapped_column(Text)
    status: Mapped[EventStatus] = mapped_column(_pg_enum(EventStatus, "event_status"))
    review_status: Mapped[ReviewStatus] = mapped_column(_pg_enum(ReviewStatus, "review_status"))
    confidence: Mapped[Decimal] = mapped_column(Numeric(3, 2))
    dedupe_key: Mapped[str] = mapped_column(Text, unique=True)
    first_seen_at: Mapped[datetime] = utc_now_column()
    deadline_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EventSource(IdMixin, TimestampMixin, Base):
    __tablename__ = "event_sources"
    __table_args__ = (UniqueConstraint("event_id", "source_url"),)

    event_id: Mapped[uuid.UUID] = mapped_column(UUID, _fk("events.id", "CASCADE"))
    # SET NULL: raw_documents are purged after 30 days; the evidence row must survive.
    raw_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, _fk("raw_documents.id", "SET NULL")
    )
    source_url: Mapped[str] = mapped_column(Text)
    snippet: Mapped[str] = mapped_column(Text)
    extracted: Mapped[dict[str, Any]] = mapped_column(JSONB)
    extractor: Mapped[str] = mapped_column(Text)


class Registration(IdMixin, TimestampMixin, Base):
    __tablename__ = "registrations"
    __table_args__ = (UniqueConstraint("user_id", "event_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(UUID, _fk("users.id", "CASCADE"))
    event_id: Mapped[uuid.UUID] = mapped_column(UUID, _fk("events.id", "CASCADE"))
    registered_at: Mapped[datetime] = utc_now_column()


class Track(IdMixin, TimestampMixin, Base):
    """'Remind me' without registering; same shape as registrations (doc 05)."""

    __tablename__ = "tracks"
    __table_args__ = (UniqueConstraint("user_id", "event_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(UUID, _fk("users.id", "CASCADE"))
    event_id: Mapped[uuid.UUID] = mapped_column(UUID, _fk("events.id", "CASCADE"))
    tracked_at: Mapped[datetime] = utc_now_column()


class CalendarItem(IdMixin, TimestampMixin, Base):
    __tablename__ = "calendar_items"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID, _fk("users.id", "CASCADE"))
    event_id: Mapped[uuid.UUID | None] = mapped_column(UUID, _fk("events.id", "SET NULL"))
    title: Mapped[str] = mapped_column(Text)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    all_day: Mapped[bool] = mapped_column(Boolean)
    color: Mapped[str | None] = mapped_column(Text)
    gcal_event_id: Mapped[str | None] = mapped_column(Text)


class CalendarFeed(IdMixin, TimestampMixin, Base):
    __tablename__ = "calendar_feeds"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID, _fk("users.id", "CASCADE"), unique=True)
    token_hash: Mapped[str] = mapped_column(Text, unique=True)


class NotificationPrefs(IdMixin, TimestampMixin, Base):
    __tablename__ = "notification_prefs"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID, _fk("users.id", "CASCADE"), unique=True)
    deadline_offsets_days: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), server_default=text("'{3,2,1,0}'")
    )
    event_offsets_days: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), server_default=text("'{3,2,1,0}'")
    )
    send_hour_local: Mapped[int] = mapped_column(SmallInteger, server_default=text("9"))
    new_event_alerts: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    new_event_filters: Mapped[dict[str, Any]] = mapped_column(JSONB)
    channels: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ChannelEndpoint(IdMixin, TimestampMixin, Base):
    __tablename__ = "channel_endpoints"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID, _fk("users.id", "CASCADE"))
    channel: Mapped[Channel] = mapped_column(channel)
    platform: Mapped[ChannelPlatform | None] = mapped_column(
        _pg_enum(ChannelPlatform, "channel_platform")
    )
    address_enc: Mapped[bytes] = mapped_column(LargeBinary)
    label: Mapped[str] = mapped_column(Text)
    is_valid: Mapped[bool] = mapped_column(Boolean)
    token_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Reminder(IdMixin, TimestampMixin, Base):
    __tablename__ = "reminders"
    __table_args__ = (
        # Each reminder is sent once (AGENTS.md: keep this key on every reminder change).
        UniqueConstraint("user_id", "event_id", "kind", "offset_days", "channel"),
        Index(
            "ix_reminders_scheduled_at_pending",
            "scheduled_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(UUID, _fk("users.id", "CASCADE"))
    event_id: Mapped[uuid.UUID] = mapped_column(UUID, _fk("events.id", "CASCADE"))
    kind: Mapped[ReminderKind] = mapped_column(_pg_enum(ReminderKind, "reminder_kind"))
    offset_days: Mapped[int] = mapped_column(SmallInteger)
    channel: Mapped[Channel] = mapped_column(channel)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[ReminderStatus] = mapped_column(_pg_enum(ReminderStatus, "reminder_status"))
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class Notification(IdMixin, TimestampMixin, Base):
    __tablename__ = "notifications"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID, _fk("users.id", "CASCADE"))
    type: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    event_id: Mapped[uuid.UUID | None] = mapped_column(UUID, _fk("events.id", "SET NULL"))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LlmProvider(IdMixin, TimestampMixin, Base):
    __tablename__ = "llm_providers"

    name: Mapped[str] = mapped_column(Text)
    type: Mapped[LlmProviderType] = mapped_column(_pg_enum(LlmProviderType, "llm_provider_type"))
    base_url: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    api_key_enc: Mapped[bytes] = mapped_column(LargeBinary)
    role: Mapped[LlmProviderRole] = mapped_column(_pg_enum(LlmProviderRole, "llm_provider_role"))
    priority: Mapped[int] = mapped_column(SmallInteger)
    input_price_per_mtok: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    output_price_per_mtok: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    daily_cap_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    monthly_cap_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    is_enabled: Mapped[bool] = mapped_column(Boolean)


class LlmUsage(IdMixin, TimestampMixin, Base):
    __tablename__ = "llm_usage"
    __table_args__ = (Index("ix_llm_usage_at", "at"),)

    provider_id: Mapped[uuid.UUID] = mapped_column(UUID, _fk("llm_providers.id"))
    job_id: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 5))
    cache_hit: Mapped[bool] = mapped_column(Boolean)
    ok: Mapped[bool] = mapped_column(Boolean)
    at: Mapped[datetime] = utc_now_column()


class LlmCache(TimestampMixin, Base):
    """Keyed by (content_hash, schema_version) per doc 05, so no surrogate id."""

    __tablename__ = "llm_cache"

    content_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    provider_id: Mapped[uuid.UUID] = mapped_column(UUID, _fk("llm_providers.id"))


class SocialSession(IdMixin, TimestampMixin, Base):
    __tablename__ = "social_sessions"

    network: Mapped[SocialNetwork] = mapped_column(social_network, unique=True)
    cookies_enc: Mapped[bytes] = mapped_column(LargeBinary)
    account_label: Mapped[str] = mapped_column(Text)
    status: Mapped[SocialSessionStatus] = mapped_column(
        _pg_enum(SocialSessionStatus, "social_session_status")
    )
    last_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditLog(IdMixin, TimestampMixin, Base):
    """Append-only. Restricting the app role to INSERT + SELECT is a separate ticket."""

    __tablename__ = "audit_log"

    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID, _fk("users.id", "SET NULL"))
    action: Mapped[str] = mapped_column(Text)
    target_type: Mapped[str] = mapped_column(Text)
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB)
    ip: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime] = utc_now_column()
