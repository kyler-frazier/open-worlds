"""Move per-game world columns into worlds.game_settings; game becomes a string.

Each game's adapter now owns its world settings as a validated JSON object, so
adding a game never adds a column. The three per-game columns move into it:

    MINECRAFT  server_type, version  -> game_settings
    PALWORLD   server_description    -> game_settings
               version (reported)    -> reported_version

Palworld's version was never chosen -- the worker records what the running
server reports -- so it moves to its own column rather than into settings.

``worlds.game`` stops being a database enum for the same reason: an enum value
per game would make installing one a schema change. The adapter registry is
what decides which values are valid.

game_port stays a column: its uniqueness across worlds is a database
constraint, and that is the guarantee that two worlds never share a port.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_world_game_settings"
down_revision = "0007_world_last_error"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "worlds",
        sa.Column(
            "game_settings",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column("worlds", sa.Column("reported_version", sa.String(64), nullable=True))

    op.execute(
        """
        UPDATE worlds
        SET game_settings = jsonb_strip_nulls(
            jsonb_build_object('server_type', server_type::text, 'version', version)
        )
        WHERE game = 'MINECRAFT'
        """
    )
    op.execute(
        """
        UPDATE worlds
        SET game_settings = CASE
                WHEN coalesce(server_description, '') = '' THEN '{}'::jsonb
                ELSE jsonb_build_object('server_description', server_description)
            END,
            reported_version = version
        WHERE game = 'PALWORLD'
        """
    )

    op.drop_column("worlds", "server_description")
    op.drop_column("worlds", "version")
    op.drop_column("worlds", "server_type")
    op.execute("DROP TYPE server_type")

    op.alter_column(
        "worlds",
        "game",
        type_=sa.String(32),
        postgresql_using="game::text",
        existing_nullable=False,
    )
    op.execute("DROP TYPE game_type")


def downgrade() -> None:
    game_type = postgresql.ENUM("MINECRAFT", "PALWORLD", name="game_type")
    game_type.create(op.get_bind())
    op.alter_column(
        "worlds",
        "game",
        type_=game_type,
        postgresql_using="game::game_type",
        existing_nullable=False,
    )

    server_type = postgresql.ENUM("VANILLA", "PAPER", "FABRIC", "FORGE", name="server_type")
    server_type.create(op.get_bind())
    op.add_column("worlds", sa.Column("server_type", server_type, nullable=True))
    op.add_column("worlds", sa.Column("version", sa.String(32), nullable=True))
    op.add_column("worlds", sa.Column("server_description", sa.Text(), nullable=True))

    op.execute(
        """
        UPDATE worlds
        SET server_type = (game_settings->>'server_type')::server_type,
            version = game_settings->>'version'
        WHERE game = 'MINECRAFT'
        """
    )
    op.execute(
        """
        UPDATE worlds
        SET server_description = nullif(game_settings->>'server_description', ''),
            version = left(reported_version, 32)
        WHERE game = 'PALWORLD'
        """
    )

    op.drop_column("worlds", "reported_version")
    op.drop_column("worlds", "game_settings")
