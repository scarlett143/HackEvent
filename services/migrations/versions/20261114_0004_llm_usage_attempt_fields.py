"""llm_usage attempt fields (HE-043)

One llm_usage row per provider attempt: `purpose` (e.g. extract/verify), `model`,
`status` (reserved | ok | rate_limited | invalid_output | error) and `latency_ms`.
A row is inserted as `reserved` with the estimated max cost before the HTTP call and
settled afterwards, so the cap check sums it while the call is in flight. All nullable:
existing rows need no backfill. Never holds prompts, page text or keys.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-14 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = (("purpose", sa.Text()), ("model", sa.Text()), ("status", sa.Text()))


def upgrade() -> None:
    for name, type_ in COLUMNS:
        op.add_column("llm_usage", sa.Column(name, type_))
    op.add_column("llm_usage", sa.Column("latency_ms", sa.Integer()))


def downgrade() -> None:
    op.drop_column("llm_usage", "latency_ms")
    for name, _ in reversed(COLUMNS):
        op.drop_column("llm_usage", name)
