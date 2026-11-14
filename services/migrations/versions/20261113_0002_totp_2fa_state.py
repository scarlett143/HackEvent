"""admin TOTP 2FA state (HE-014)

users.totp_enabled_at (null = not enrolled / enrolment pending), users.totp_last_step
(last accepted RFC 6238 time-step, replay guard), sessions.totp_verified_at (this session
passed 2FA). All nullable, so existing rows need no backfill.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-13 23:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("totp_enabled_at", sa.DateTime(timezone=True)))
    op.add_column("users", sa.Column("totp_last_step", sa.BigInteger()))
    op.add_column("sessions", sa.Column("totp_verified_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("sessions", "totp_verified_at")
    op.drop_column("users", "totp_last_step")
    op.drop_column("users", "totp_enabled_at")
