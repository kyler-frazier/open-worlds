"""SQLAlchemy models. The database is the source of truth; containers are
rebuilt from it."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Role(str, enum.Enum):
    USER = "USER"
    ADMIN = "ADMIN"


class CollaboratorLevel(str, enum.Enum):
    """Per-world access for someone other than the owner. No OWNER member: the
    owner is ``worlds.owner_id``, not a row here."""

    #: Start, stop and wake. Nothing else.
    OPERATOR = "OPERATOR"
    #: The above plus settings. Cannot delete, replace contents, or use the
    #: owner's Drive.
    MANAGER = "MANAGER"


class WorldState(str, enum.Enum):
    """Persisted lifecycle intent. Whether a RUNNING world is joinable is
    derived separately (``readiness``). The database enum also has an unused
    legacy CREATING value."""

    RUNNING = "RUNNING"
    SLEEPING = "SLEEPING"
    # Shutting down gracefully (minutes for some games), then OFF or SLEEPING
    # per ``World.stop_target``. Persisted because the container is still
    # running meanwhile, and a reconcile would otherwise call it RUNNING.
    STOPPING = "STOPPING"
    OFF = "OFF"
    ERROR = "ERROR"
    DELETING = "DELETING"


class BackupKind(str, enum.Enum):
    AUTO = "AUTO"
    MANUAL = "MANUAL"


class BackupStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class AutoBackupInterval(str, enum.Enum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"


class JobType(str, enum.Enum):
    BACKUP = "BACKUP"
    IMPORT = "IMPORT"
    CONTENT = "CONTENT"


class ContentKind(str, enum.Enum):
    """A curated content kind; each is installed differently."""

    MOD = "MOD"
    DATAPACK = "DATAPACK"
    RESOURCEPACK = "RESOURCEPACK"


class ContentSource(str, enum.Enum):
    """Where a catalog entry's files are resolved from."""

    MODRINTH = "MODRINTH"
    # Admin-supplied direct URL + hash, for content not on Modrinth.
    PINNED = "PINNED"


class JobStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    # Google's stable subject id: what the allow-list keys on.
    google_sub: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[Role] = mapped_column(SAEnum(Role, name="role"), default=Role.USER, nullable=False)

    # Encrypted Google Drive refresh token; NULL until the user opts into backups.
    drive_refresh_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    drive_linked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Copied into the session cookie and checked on every request. Bumping it
    # signs the user out everywhere: the only lever a signed cookie offers.
    session_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    worlds: Mapped[list[World]] = relationship(back_populates="owner", cascade="all, delete-orphan")
    # Grants on other people's worlds. foreign_keys is required: two FKs from
    # WorldCollaborator into users make the join ambiguous.
    collaborations: Mapped[list[WorldCollaborator]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="WorldCollaborator.user_id",
    )


class World(Base, TimestampMixin):
    __tablename__ = "worlds"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Globally unique DNS label: the subdomain and the container name.
    subdomain: Mapped[str] = mapped_column(String(63), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)

    # An adapter id (MINECRAFT). A string, not an enum, so adding a game needs
    # no migration.
    game: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # The owner's choices only this game has, validated by the adapter's
    # settings_model on every write.
    game_settings: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # What the running server last reported, for games that cannot pin a
    # version. Observed, so not a setting.
    reported_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Host port for games whose protocol carries no hostname. A column, not a
    # setting, so the unique constraint guarantees no two worlds share one.
    game_port: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True, index=True)

    memory_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    max_players: Mapped[int] = mapped_column(Integer, default=20, nullable=False)

    state: Mapped[WorldState] = mapped_column(
        SAEnum(WorldState, name="world_state"), default=WorldState.OFF, nullable=False, index=True
    )
    # While STOPPING: OFF (container removed) or SLEEPING (kept, wakeable). Lets
    # the sweep finish a stop whose API process died.
    stop_target: Mapped[WorldState | None] = mapped_column(
        SAEnum(WorldState, name="world_state"), nullable=True
    )
    # RCON (Minecraft) or REST (Palworld) password, platform-generated and
    # never shown. Encrypted.
    admin_password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Join password, for games that have one. NULL or empty: open. Encrypted,
    # but shown to the owner and admins to pass on.
    server_password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Advisory: stale unless RUNNING and the last poll worked.
    player_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    players_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When it last had a player on. Not players_updated_at, which every poll
    # refreshes: idle-sleep needs how long it has been empty. NULL falls back to
    # started_at.
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Running is not joinable: both are reset on every start. ready_at is when
    # the admin interface first answered, the only real evidence (Palworld's
    # Docker healthcheck passes long before the world loads).
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Opt-in; needs a linked Drive.
    auto_backup_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    auto_backup_interval: Mapped[AutoBackupInterval] = mapped_column(
        SAEnum(AutoBackupInterval, name="auto_backup_interval"),
        default=AutoBackupInterval.DAILY,
        nullable=False,
    )
    auto_backup_max: Mapped[int] = mapped_column(Integer, default=7, nullable=False)

    # Blocking off: the guard records but blocks nothing. Set at creation from
    # PLATFORM_EGRESS_BLOCKING_DEFAULT.
    egress_log_only: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )

    # Why the latest boot failed: a redacted log excerpt (startup_errors.py).
    # Cleared when the world reaches RUNNING; the UI notifies on last_error_at.
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_auto_backup_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Backoff for a failing schedule (a revoked or full Drive), which was
    # otherwise retried on every tick. Success resets it.
    auto_backup_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    auto_backup_last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Of the data dir at the last AUTO backup; an unchanged world is skipped.
    last_auto_backup_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)

    owner: Mapped[User] = relationship(back_populates="worlds")
    backups: Mapped[list[Backup]] = relationship(
        back_populates="world", cascade="all, delete-orphan"
    )
    collaborators: Mapped[list[WorldCollaborator]] = relationship(
        back_populates="world",
        cascade="all, delete-orphan",
        foreign_keys="WorldCollaborator.world_id",
    )


class WorldCollaborator(Base, TimestampMixin):
    """Someone other than the owner who may act on a world. One row per
    (world, person); the add route relies on the unique constraint."""

    __tablename__ = "world_collaborators"
    __table_args__ = (UniqueConstraint("world_id", "user_id", name="uq_world_collaborator"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    world_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("worlds.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    level: Mapped[CollaboratorLevel] = mapped_column(
        SAEnum(CollaboratorLevel, name="collaborator_level"), nullable=False
    )
    # Audit only. SET NULL: deleting a user must not revoke grants they made.
    added_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    world: Mapped[World] = relationship(back_populates="collaborators", foreign_keys=[world_id])
    user: Mapped[User] = relationship(back_populates="collaborations", foreign_keys=[user_id])
    # No relationship for added_by_id: display-only, resolved by id.


class Backup(Base, TimestampMixin):
    __tablename__ = "backups"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    world_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("worlds.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[BackupKind] = mapped_column(SAEnum(BackupKind, name="backup_kind"), nullable=False)
    status: Mapped[BackupStatus] = mapped_column(
        SAEnum(BackupStatus, name="backup_status"), default=BackupStatus.PENDING, nullable=False
    )

    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    drive_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    world: Mapped[World] = relationship(back_populates="backups")


class StagedUpload(Base, TimestampMixin):
    """An upload staged at preflight, awaiting commit or cancellation. One per
    world, which serialises preflight and commit."""

    __tablename__ = "staged_uploads"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    world_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("worlds.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    # On the shared staging volume.
    path: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # JSON list of prunable top-level entries.
    unknown_entries: Mapped[str] = mapped_column(Text, nullable=False, default="[]")


class Job(Base, TimestampMixin):
    """A durable job queue, consumed by the worker."""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    type: Mapped[JobType] = mapped_column(SAEnum(JobType, name="job_type"), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        SAEnum(JobStatus, name="job_status"), default=JobStatus.PENDING, nullable=False, index=True
    )
    world_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("worlds.id", ondelete="CASCADE"), nullable=True, index=True
    )
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ContentCatalog(Base, TimestampMixin):
    """Admin-curated installable content: Minecraft only, from Modrinth.

    Stores the project, never a version: the file is resolved per world at
    install time. PINNED entries carry an explicit URL and hash instead.
    """

    __tablename__ = "content_catalog"
    __table_args__ = (
        UniqueConstraint("kind", "project_slug", name="uq_content_catalog_kind_slug"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    kind: Mapped[ContentKind] = mapped_column(
        SAEnum(ContentKind, name="content_kind"), nullable=False, index=True
    )
    source: Mapped[ContentSource] = mapped_column(
        SAEnum(ContentSource, name="content_source"),
        default=ContentSource.MODRINTH,
        nullable=False,
    )
    # Modrinth slug, or a synthetic id for PINNED.
    project_slug: Mapped[str] = mapped_column(String(120), nullable=False)
    # Base62 id, matched against mods' required dependencies.
    modrinth_project_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    icon_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # JSON lists for filtering the pickers. Advisory for MODRINTH (install-time
    # resolution decides), binding for PINNED.
    cached_loaders: Mapped[str | None] = mapped_column(Text, nullable=True)
    cached_game_versions: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Modrinth's client/server side. Optional on the client: "server-side only".
    cached_client_side: Mapped[str | None] = mapped_column(String(16), nullable=True)
    cached_server_side: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # --- PINNED-only fields (NULL for MODRINTH entries) ---
    pinned_download_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    pinned_sha512: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Resource packs only (server.properties wants sha1).
    pinned_sha1: Mapped[str | None] = mapped_column(String(40), nullable=True)

    installs: Mapped[list[WorldContent]] = relationship(
        back_populates="catalog", cascade="all, delete-orphan"
    )


class WorldContent(Base, TimestampMixin):
    """A catalog entry installed on a world. Its ``sha512`` marks jars as
    platform-managed for backups and imports. Resource packs are served by URL,
    one per world."""

    __tablename__ = "world_content"
    __table_args__ = (
        UniqueConstraint("world_id", "catalog_id", name="uq_world_content_world_catalog"),
        # The server can enforce only one resource pack.
        Index(
            "uq_world_content_one_resourcepack",
            "world_id",
            unique=True,
            postgresql_where=text("kind = 'RESOURCEPACK'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    world_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("worlds.id", ondelete="CASCADE"), nullable=False, index=True
    )
    catalog_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("content_catalog.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Denormalized, to filter by kind without the catalog.
    kind: Mapped[ContentKind] = mapped_column(
        SAEnum(ContentKind, name="content_kind"), nullable=False
    )

    # NULL for pinned.
    version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Under mods/, plugins/ or datapacks/; NULL for resource packs.
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sha512: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sha1: Mapped[str | None] = mapped_column(String(40), nullable=True)
    download_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Resource packs: clients must accept it.
    enforce: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    world: Mapped[World] = relationship()
    catalog: Mapped[ContentCatalog] = relationship(back_populates="installs")


class PortRelease(Base):
    """When a game port was last released, and by whom.

    Players bookmark addresses, so a freed port goes to the back of the reuse
    queue. Except for the same world recreated (same subdomain and owner),
    which gets its port back.
    """

    __tablename__ = "port_releases"

    port: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    released_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Nullable: older rows have no claimant, and a deleted owner is SET NULL
    # so the row keeps its place in the queue.
    subdomain: Mapped[str | None] = mapped_column(String(63), nullable=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (Index("ix_port_releases_claimant", "subdomain", "owner_id"),)


class SignedOutSession(Base):
    """A session cookie signed out before it expired.

    A signed cookie cannot be recalled, so logout records the cookie's id and
    every request checks it here. Kept only until the cookie would have
    expired anyway.
    """

    __tablename__ = "signed_out_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
