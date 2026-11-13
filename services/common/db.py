"""SQLAlchemy declarative base shared by all models (doc 05 conventions).

Every table gets `id uuid PK default gen_random_uuid()` and `created_at` / `updated_at`
timestamptz columns (UTC). Constraint names follow `NAMING_CONVENTION` so the hand-written
Alembic migrations and the models agree on names.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utc_now_column(*, onupdate: bool = False, sort_order: int = 0) -> Mapped[datetime]:
    """A NOT NULL timestamptz defaulting to now() in the database."""
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now() if onupdate else None,
        sort_order=sort_order,
    )


class IdMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        sort_order=-100,
    )


class TimestampMixin:
    created_at: Mapped[datetime] = utc_now_column(sort_order=100)
    updated_at: Mapped[datetime] = utc_now_column(onupdate=True, sort_order=100)
