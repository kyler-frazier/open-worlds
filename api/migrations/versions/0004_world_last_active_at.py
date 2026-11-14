"""Add worlds.last_active_at.

Idle-sleep needs "how long has this world been empty". It was using
players_updated_at, which is "when we last polled" and is refreshed on every
successful poll -- so the comparison could never be true and Minecraft worlds
never slept. This is the timestamp that actually means what the query wanted.

Nullable with no backfill on purpose: NULL reads as "never seen occupied", and
the query coalesces to started_at, which is the right answer for every existing
row. Backfilling now() would instead grant every running world a fresh idle
window it has not earned.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_world_last_active_at"
down_revision = "0003_world_collaborators"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "worlds",
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("worlds", "last_active_at")
