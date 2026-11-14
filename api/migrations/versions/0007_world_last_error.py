"""Add worlds.last_error and worlds.last_error_at.

Why a world's server failed to start, from its log, so the dashboard can say
so instead of showing "starting" (and later "unresponsive") with no reason.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_world_last_error"
down_revision = "0006_existing_worlds_egress_open"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("worlds", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column("worlds", sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("worlds", "last_error_at")
    op.drop_column("worlds", "last_error")
