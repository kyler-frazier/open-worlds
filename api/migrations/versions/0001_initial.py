"""Initial schema.

Squashed baseline. Replaces the 0001..0006 chain, which was collapsed once the
last test world was deleted and there was no deployed data left to migrate.
This reproduces exactly the schema that chain produced, verified by diffing a
``pg_dump --schema-only`` of both, with one deliberate exception: ``CREATING``
is gone from ``world_state``. It was never written by any code path and was
removed from the enum in the application, so carrying it into a fresh database
would recreate drift on day one.

The shape worth noting is how per-game fields are handled. ``worlds.game`` is the
discriminator and is NOT NULL; fields that only some games have are nullable and
owned by that game's adapter -- ``version``/``server_type`` for Minecraft,
``game_port`` for Palworld. ``game_port`` is additionally UNIQUE, because for a
game whose protocol carries no hostname the port is the only thing that tells one
world from another. ``port_releases`` keeps a freed port from being recycled
straight into a different world.

Revision ID: 0001_initial
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    role = sa.Enum("USER", "ADMIN", name="role")
    game_type = sa.Enum("MINECRAFT", "PALWORLD", name="game_type")
    server_type = sa.Enum("VANILLA", "PAPER", "FABRIC", "FORGE", name="server_type")
    content_kind = sa.Enum("MOD", "DATAPACK", "RESOURCEPACK", name="content_kind")
    content_source = sa.Enum("MODRINTH", "PINNED", name="content_source")
    world_state = sa.Enum(
        "RUNNING", "SLEEPING", "STOPPING", "OFF", "ERROR", "DELETING", name="world_state"
    )
    backup_kind = sa.Enum("AUTO", "MANUAL", name="backup_kind")
    backup_status = sa.Enum("PENDING", "RUNNING", "SUCCESS", "FAILED", name="backup_status")
    auto_backup_interval = sa.Enum("DAILY", "WEEKLY", "MONTHLY", name="auto_backup_interval")
    job_type = sa.Enum("BACKUP", "IMPORT", "CONTENT", name="job_type")
    job_status = sa.Enum("PENDING", "RUNNING", "SUCCESS", "FAILED", name="job_status")

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("google_sub", sa.String(255), nullable=False, unique=True),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("role", role, nullable=False, server_default="USER"),
        sa.Column("drive_refresh_token_enc", sa.Text(), nullable=True),
        sa.Column("drive_linked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("session_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_users_google_sub", "users", ["google_sub"])

    op.create_table(
        "worlds",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("subdomain", sa.String(63), nullable=False, unique=True),
        sa.Column("display_name", sa.String(120), nullable=False),
        # Which game this world runs. The discriminator every adapter keys off.
        sa.Column("game", game_type, nullable=False),
        # Minecraft only: server flavour and pinned version. NULL elsewhere.
        sa.Column("server_type", server_type, nullable=True),
        sa.Column("version", sa.String(32), nullable=True),
        # Palworld only: the world's own UDP port, unique platform-wide. With no
        # hostname in its protocol the port is the only thing distinguishing
        # worlds. NULL for hostname-routed games.
        sa.Column("game_port", sa.Integer(), nullable=True, unique=True),
        sa.Column("memory_mb", sa.Integer(), nullable=False),
        sa.Column("max_players", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("state", world_state, nullable=False, server_default="OFF"),
        sa.Column("admin_password_enc", sa.Text(), nullable=True),
        # Optional shared join password, for games whose server has one
        # (Palworld). Encrypted like the admin password, but unlike it this is
        # read back and shown to the owner. NULL/empty means an open world.
        sa.Column("server_password_enc", sa.Text(), nullable=True),
        sa.Column("player_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("players_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auto_backup_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "auto_backup_interval", auto_backup_interval, nullable=False, server_default="DAILY"
        ),
        sa.Column("auto_backup_max", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("last_auto_backup_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_auto_backup_fingerprint", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auto_backup_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("auto_backup_last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stop_target", world_state, nullable=True),
    )
    op.create_index("ix_worlds_owner_id", "worlds", ["owner_id"])
    op.create_index("ix_worlds_subdomain", "worlds", ["subdomain"])
    op.create_index("ix_worlds_game_port", "worlds", ["game_port"])
    op.create_index("ix_worlds_game", "worlds", ["game"])
    op.create_index("ix_worlds_state", "worlds", ["state"])

    # Timestamped record of when each port was handed back, so allocation can
    # prefer never-used ports and then the longest-released one. Without this a
    # deleted world's port would be reissued immediately and every saved address
    # pointing at it would silently reach a different world.
    op.create_table(
        "port_releases",
        sa.Column("port", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("subdomain", sa.String(63), nullable=True),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name="fk_port_releases_owner_id", ondelete="SET NULL"
        ),
    )
    # Reclaim looks up by (subdomain, owner) and takes the most recent match.
    op.create_index("ix_port_releases_claimant", "port_releases", ["subdomain", "owner_id"])

    op.create_table(
        "backups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "world_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("worlds.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", backup_kind, nullable=False),
        sa.Column("status", backup_status, nullable=False, server_default="PENDING"),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("drive_file_id", sa.String(255), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_backups_world_id", "backups", ["world_id"])

    op.create_table(
        "staged_uploads",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "world_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("worlds.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("unknown_entries", sa.Text(), nullable=False, server_default="[]"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("type", job_type, nullable=False),
        sa.Column("status", job_status, nullable=False, server_default="PENDING"),
        sa.Column(
            "world_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("worlds.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_world_id", "jobs", ["world_id"])

    # Curated content: Minecraft-only today, because it depends on Modrinth
    # being a keyless trusted registry. Adapters without one never write here.
    op.create_table(
        "content_catalog",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", content_kind, nullable=False),
        sa.Column("source", content_source, nullable=False, server_default="MODRINTH"),
        sa.Column("project_slug", sa.String(120), nullable=False),
        sa.Column("modrinth_project_id", sa.String(32), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("icon_url", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("cached_loaders", sa.Text(), nullable=True),
        sa.Column("cached_game_versions", sa.Text(), nullable=True),
        sa.Column("cached_client_side", sa.String(16), nullable=True),
        sa.Column("cached_server_side", sa.String(16), nullable=True),
        sa.Column("pinned_download_url", sa.Text(), nullable=True),
        sa.Column("pinned_sha512", sa.String(128), nullable=True),
        sa.Column("pinned_sha1", sa.String(40), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("kind", "project_slug", name="uq_content_catalog_kind_slug"),
    )
    op.create_index("ix_content_catalog_kind", "content_catalog", ["kind"])

    op.create_table(
        "world_content",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "world_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("worlds.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "catalog_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("content_catalog.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", content_kind, nullable=False),
        sa.Column("version_id", sa.String(64), nullable=True),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("sha512", sa.String(128), nullable=True),
        sa.Column("sha1", sa.String(40), nullable=True),
        sa.Column("download_url", sa.Text(), nullable=True),
        sa.Column("enforce", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "installed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("world_id", "catalog_id", name="uq_world_content_world_catalog"),
    )
    op.create_index("ix_world_content_world_id", "world_content", ["world_id"])
    op.create_index("ix_world_content_catalog_id", "world_content", ["catalog_id"])
    # At most one resource pack per world (the server can only enforce one).
    op.create_index(
        "uq_world_content_one_resourcepack",
        "world_content",
        ["world_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'RESOURCEPACK'"),
    )


def downgrade() -> None:
    op.drop_table("world_content")
    op.drop_table("content_catalog")
    op.drop_table("jobs")
    op.drop_table("staged_uploads")
    op.drop_table("backups")
    op.drop_table("port_releases")
    op.drop_table("worlds")
    op.drop_table("users")
    for enum in (
        "content_source",
        "content_kind",
        "job_status",
        "job_type",
        "auto_backup_interval",
        "backup_status",
        "backup_kind",
        "world_state",
        "server_type",
        "game_type",
        "role",
    ):
        sa.Enum(name=enum).drop(op.get_bind(), checkfirst=True)
