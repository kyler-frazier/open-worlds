"""Record signed-out session cookies, so logout ends the cookie it was sent
with rather than only asking the browser to forget it."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_signed_out_sessions"
down_revision = "0008_world_game_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signed_out_sessions",
        sa.Column("session_id", sa.String(64), primary_key=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_signed_out_sessions_expires_at", "signed_out_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_signed_out_sessions_expires_at", table_name="signed_out_sessions")
    op.drop_table("signed_out_sessions")
