"""Hourly crawl for aggregators and tier 1 sources (owner decision 2026-11-19)

`sources.cadence_hours` is stored (workers.scheduler.cadence_hours_for sets it on create
and tier change), so existing rows need the new value too. Data only; downgrade restores
the old defaults (4 h aggregators, 24 h tier 1).

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-19 01:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIER1 = (
    "kind <> 'aggregator' AND COALESCE(tier_override,"
    " (SELECT tier FROM organisations o WHERE o.id = organisation_id)) = 1"
)


def _set(aggregator: int, tier1: int) -> None:
    op.execute(f"UPDATE sources SET cadence_hours = {aggregator} WHERE kind = 'aggregator'")
    op.execute(f"UPDATE sources SET cadence_hours = {tier1} WHERE {TIER1}")


def upgrade() -> None:
    _set(1, 1)
    # Pull far-off next runs in, so the new cadence applies within the hour.
    op.execute(
        "UPDATE sources SET next_run_at = now() + interval '1 hour'"
        " WHERE cadence_hours = 1 AND next_run_at > now() + interval '1 hour'"
    )


def downgrade() -> None:
    _set(4, 24)
