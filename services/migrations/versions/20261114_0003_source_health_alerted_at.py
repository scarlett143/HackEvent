"""source health alerted state (HE-031)

sources.health_alerted_at: set when the source turned red and Admins were alerted,
cleared when it is green again, so a source that stays red is alerted only once.
Nullable, so existing rows need no backfill. error_streak / empty_streak already exist
(0001).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-14 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("health_alerted_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("sources", "health_alerted_at")
