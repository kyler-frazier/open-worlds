"""Add world_collaborators.

Lets an owner grant someone else access to one world without handing over their
account or making them a platform admin. One row per (world, person); changing a
tier is an UPDATE, which the unique constraint enforces.

The enum carries only the levels that can be stored. There is no OWNER member:
the owner is worlds.owner_id, not a membership. Adding an unreachable value is
the mistake 0001 deliberately corrected by dropping CREATING from world_state.

added_by_id is SET NULL rather than CASCADE on purpose -- deleting the admin who
granted access must not silently revoke the access they granted.

Revision ID: 0003_world_collaborators
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_world_collaborators"
down_revision = "0002_world_server_description"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Declared here and passed to create_table, matching 0001: SQLAlchemy emits
    # the CREATE TYPE as part of the table creation.
    collaborator_level = sa.Enum("OPERATOR", "MANAGER", name="collaborator_level")

    op.create_table(
        "world_collaborators",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "world_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("worlds.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("level", collaborator_level, nullable=False),
        sa.Column(
            "added_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("world_id", "user_id", name="uq_world_collaborator"),
    )
    # The unique constraint already indexes (world_id, user_id), so world_id
    # lookups are covered by its leading column. user_id needs its own: the
    # dashboard asks "which worlds is this person on" on every poll.
    op.create_index(
        "ix_world_collaborators_user_id", "world_collaborators", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_world_collaborators_user_id", table_name="world_collaborators")
    op.drop_table("world_collaborators")
    sa.Enum(name="collaborator_level").drop(op.get_bind(), checkfirst=True)
