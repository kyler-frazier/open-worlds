"""Add worlds.egress_log_only.

The per-world "Allow unknown network connections" switch: a guarded world whose
guard records but does not block. Defaults false for every existing row, which
is the enforced state -- and it is only honoured while PLATFORM_EGRESS_OVERRIDE_ACTORS
permits overrides at all, so the column alone can never loosen anything.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_world_egress_log_only"
down_revision = "0004_world_last_active_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "worlds",
        sa.Column("egress_log_only", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("worlds", "egress_log_only")
