"""channel enum gains `in_app` (HE-070)

Reminders are planned per channel and the in-app notification centre is one (PRD §4.4,
doc 06 notifier plugins; DEFAULT_CHANNELS already enables it), so `reminders.channel`
must be able to hold it. Additive: no rows change.

Downgrade rebuilds the type without the value; rows that use it are deleted first
(reminders are recomputed from registrations / tracks, nothing is lost for good).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-18 12:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = ("fcm", "email", "telegram", "ntfy", "gcal")
USERS = (("reminders", "channel"), ("channel_endpoints", "channel"))


def upgrade() -> None:
    op.execute("ALTER TYPE channel ADD VALUE IF NOT EXISTS 'in_app'")


def downgrade() -> None:
    for table, column in USERS:
        op.execute(f"DELETE FROM {table} WHERE {column}::text = 'in_app'")
    op.execute("ALTER TYPE channel RENAME TO channel_old")
    values = ", ".join(f"'{v}'" for v in OLD)
    op.execute(f"CREATE TYPE channel AS ENUM ({values})")
    for table, column in USERS:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} TYPE channel USING {column}::text::channel"
        )
    op.execute("DROP TYPE channel_old")
