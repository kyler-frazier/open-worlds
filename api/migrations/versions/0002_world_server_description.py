"""Add worlds.server_description.

Palworld's in-game server blurb becomes owner-editable, so it needs somewhere to
live. Nullable and unencrypted: NULL and "" both mean unset, and the value is
published to every client that can see the server, so there is nothing to
protect. Existing worlds get NULL and keep rendering no description until their
owner sets one.

Revision ID: 0002_world_server_description
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_world_server_description"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("worlds", sa.Column("server_description", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("worlds", "server_description")
