"""Pydantic request/response schemas for the API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..common.authz import AccessLevel
from ..common.models import (
    AutoBackupInterval,
    BackupKind,
    BackupStatus,
    CollaboratorLevel,
    ContentKind,
    ContentSource,
    JobStatus,
    JobType,
    Role,
    WorldState,
)
from ..common.readiness import Readiness


class MeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    google_sub: str
    email: str | None
    display_name: str | None
    role: Role
    drive_linked: bool
    is_admin: bool


class PendingOut(BaseModel):
    status: str = "pending"
    google_sub: str
    message: str = "Your access request is pending admin approval."


class WorldCreate(BaseModel):
    """A new world. Fields that depend on the game are checked by its adapter,
    not here."""

    game: str = Field(min_length=1, max_length=32)
    display_name: str = Field(min_length=1, max_length=120)
    subdomain: str = Field(min_length=1, max_length=63)
    # Neither game boots in less.
    memory_mb: int | None = Field(default=None, ge=1024, le=65536)
    max_players: int | None = Field(default=None, ge=1, le=64)

    # Games with supports_server_password only. Empty: open.
    server_password: str = Field(default="", max_length=128)
    # The adapter's settings_model; it fills in what is left out.
    settings: dict[str, Any] = Field(default_factory=dict)


class WorldUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    memory_mb: int | None = Field(default=None, ge=1024, le=65536)
    max_players: int | None = Field(default=None, ge=1, le=64)
    # "" clears it (open world); None leaves it alone.
    server_password: str | None = Field(default=None, max_length=128)
    # Keys left out keep their value; validated as a whole after merging.
    settings: dict[str, Any] | None = None
    auto_backup_enabled: bool | None = None
    auto_backup_interval: AutoBackupInterval | None = None
    auto_backup_max: int | None = Field(default=None, ge=1, le=100)
    # Allowed by PLATFORM_EGRESS_OVERRIDE_ACTORS; applies at the next start.
    egress_log_only: bool | None = None
    # Any change must be confirmed; the UI asks first, this enforces it.
    confirm: bool = False


class IconFileOut(BaseModel):
    """One icon file a world has, for showing what clearing it would delete."""

    #: "server": the world's own; "save": Minecraft's save-folder icon.
    kind: Literal["server", "save"]
    path: str
    #: A 64x64 PNG, i.e. whether the game shows it.
    valid: bool
    version: int


class WorldOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    game: str
    subdomain: str
    display_name: str
    memory_mb: int
    max_players: int
    state: WorldState
    # Port-allocating games only.
    game_port: int | None = None
    # In clear, for the owner to pass on. Only built for the owner or an admin.
    server_password: str = ""
    settings: dict[str, Any] = Field(default_factory=dict)
    # For games that cannot pin a version.
    reported_version: str | None = None
    # Advisory: stale unless RUNNING and the last poll worked.
    player_count: int = 0
    players_updated_at: datetime | None = None
    # Whether a RUNNING world is joinable yet; servers take a while to boot.
    readiness: Readiness = Readiness.NOT_RUNNING
    auto_backup_enabled: bool
    auto_backup_interval: AutoBackupInterval
    auto_backup_max: int
    # Guarded unless the emergency switch is off; log_only is blocking off.
    egress_guarded: bool = False
    egress_log_only: bool = False
    # Blocking off where the platform blocks by default: worth flagging.
    egress_open_exception: bool = False
    # Why the latest boot failed (the line that matters, then the excerpt).
    # The UI notifies on last_error_at.
    last_error: str | None = None
    last_error_summary: str | None = None
    last_error_at: datetime | None = None
    created_at: datetime
    # For sorting. last_active_at: last poll that found a player.
    last_active_at: datetime | None = None
    started_at: datetime | None = None
    connect_address: str
    # Always populated, so the UI never infers ownership from absence.
    owner_email: str | None = None
    owner_id: uuid.UUID
    #: What the caller may do; the UI gates on this rather than guessing.
    access_level: AccessLevel
    #: When the icon last changed (ms), or None without one. Busts caches.
    icon_version: int | None = None
    #: The world's own (removable), not the save folder's.
    icon_is_custom: bool = False


class BackupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: BackupKind
    status: BackupStatus
    filename: str
    size_bytes: int | None
    created_at: datetime
    completed_at: datetime | None
    error: str | None


class BackupCreate(BaseModel):
    kind: BackupKind = BackupKind.MANUAL


class RestorableBackupOut(BaseModel):
    """A successful, Drive-stored backup the user can import into a new world."""

    id: uuid.UUID
    world_id: uuid.UUID
    # So the UI never offers a Minecraft save to a Palworld world.
    game: str
    world_display_name: str
    world_subdomain: str
    kind: BackupKind
    filename: str
    size_bytes: int | None
    created_at: datetime


class DriveBackupOut(BaseModel):
    """An archive in the user's Drive. ``known`` is false for one whose world
    (and backup row) was deleted: the folder name is all that is left."""

    drive_file_id: str
    filename: str
    world_subdomain: str
    # From the folder it sits in; None for archives from before the layout
    # named the game.
    game: str | None
    size_bytes: int | None
    modified_at: datetime | None
    known: bool


class ImportFromDrive(BaseModel):
    # Exactly one: a tracked backup, or a Drive file whose world was deleted.
    backup_id: uuid.UUID | None = None
    drive_file_id: str | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> ImportFromDrive:
        if (self.backup_id is None) == (self.drive_file_id is None):
            raise ValueError("Provide exactly one of backup_id or drive_file_id.")
        return self


class RouteStatusOut(BaseModel):
    exists: bool
    # For diagnostics.
    state: str | None = None


class WakeTargetOut(BaseModel):
    """A world the routers may need to wake. Only port-allocating games have a
    ``game_port``."""

    subdomain: str
    game_port: int | None = None
    state: str
    # host:port on the games network, where the placeholder relays a player.
    backend: str | None = None
    # How long the server itself has reported no players, when its count is
    # fresh; None when unknown. pal-waker sleeps on this rather than on raw
    # UDP, which anyone can send from a spoofed address.
    players_idle_seconds: float | None = None


class WakeTargetsOut(BaseModel):
    targets: list[WakeTargetOut]


class GameOut(BaseModel):
    """What one game supports, so the UI can build the right form for it."""

    game: str
    display_name: str
    supports_version_pinning: bool
    supports_content_catalog: bool
    supports_server_password: bool
    allocates_port: bool
    # JSON schema of the game's settings, with runtime defaults filled in.
    settings_schema: dict[str, Any]
    # Fields and settings keys that change only while a world is off.
    spec_fields: list[str]
    # Operator-supplied; the query busts caches.
    art_url: str | None = None
    # MiB for a new world that names none, clamped to the host's bounds.
    default_memory_mb: int
    # When it allocates ports. None: unlimited.
    ports_available: int | None = None


class LimitsOut(BaseModel):
    max_worlds_per_user: int
    max_upload_bytes: int
    # The host's bounds; each game's default is on GameOut.
    max_memory_mb: int
    min_memory_mb: int
    default_max_players: int
    max_players_cap: int
    # For example connect addresses on the create form.
    servers_domain: str
    # PLATFORM_EGRESS_OVERRIDE_ACTORS.
    egress_override_actors: Literal["none", "admin", "owner_and_manager"] = "none"
    egress_blocking_default: bool = False
    games: list[GameOut]


class IconChangeOut(BaseModel):
    """What an import will do with a save folder's icon, said before committing."""

    #: "delete": server-icon.png exists, so the save's is never shown.
    #: "move": the save's becomes server-icon.png.
    action: Literal["delete", "move"]
    path: str
    to: str | None = None


class StagedUploadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    size_bytes: int
    file_count: int
    unknown_entries: list[str]
    # The settings this archive carries, to review before committing.
    settings: dict[str, str] = {}
    # Icon files the import will move or delete, leaving one.
    icon_changes: list[IconChangeOut] = []


class ImportCommit(BaseModel):
    # Drop the entries preflight reported as prunable.
    prune: bool = False


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    google_sub: str
    email: str | None
    display_name: str | None
    role: Role
    drive_linked: bool
    created_at: datetime


class AddUserIn(BaseModel):
    google_sub: str = Field(min_length=3, max_length=255)
    role: Role = Role.USER


_MIN_UPLOAD_BYTES = 1024 * 1024


class RuntimeSettingsOut(BaseModel):
    max_upload_bytes: int
    max_upload_file_count: int
    extra_allowed_root_names: list[str]


class AdminGameOut(BaseModel):
    """One installed game, as the admin's Games settings show it."""

    game: str
    display_name: str
    enabled: bool
    world_count: int
    #: What this host lacks for the game to be offered.
    problems: list[str]


class AdminGamesUpdate(BaseModel):
    #: Replaces the whole list.
    enabled: list[str]


class RuntimeSettingsUpdate(BaseModel):
    max_upload_bytes: int | None = Field(default=None, ge=_MIN_UPLOAD_BYTES)
    max_upload_file_count: int | None = Field(default=None, ge=1)
    extra_allowed_root_names: list[str] | None = None


class ContentCatalogOut(BaseModel):
    """An admin-curated catalog entry (mod / datapack / resource pack)."""

    id: uuid.UUID
    kind: ContentKind
    source: ContentSource
    project_slug: str
    name: str
    icon_url: str | None = None
    enabled: bool
    loaders: list[str] = Field(default_factory=list)
    game_versions: list[str] = Field(default_factory=list)
    created_at: datetime


class ContentCatalogCreate(BaseModel):
    kind: ContentKind
    source: ContentSource = ContentSource.MODRINTH
    # MODRINTH: the project slug; metadata is fetched. PINNED: any unique id,
    # and the fields below are required.
    project_slug: str = Field(min_length=1, max_length=120)
    name: str | None = Field(default=None, max_length=200)
    loaders: list[str] | None = None
    game_versions: list[str] | None = None
    download_url: str | None = None
    sha512: str | None = Field(default=None, max_length=128)
    sha1: str | None = Field(default=None, max_length=40)


class ContentCatalogUpdate(BaseModel):
    enabled: bool | None = None


class WorldContentOut(BaseModel):
    """A catalog entry installed on a world."""

    id: uuid.UUID
    catalog_id: uuid.UUID
    kind: ContentKind
    name: str
    icon_url: str | None = None
    filename: str | None = None
    version_id: str | None = None
    installed_at: datetime


class AvailableContentOut(BaseModel):
    """A catalog entry offered for a specific world (already filtered for fit)."""

    catalog_id: uuid.UUID
    kind: ContentKind
    source: ContentSource
    name: str
    icon_url: str | None = None
    installed: bool = False
    # Mods: no client-side install needed.
    server_side_only: bool | None = None
    # Installed resource packs: whether clients must accept it.
    enforce: bool | None = None


class ContentApplyIn(BaseModel):
    catalog_id: uuid.UUID
    # Resource packs: clients must accept it.
    enforce: bool = False


class MessageOut(BaseModel):
    message: str


class JobAcceptedOut(MessageOut):
    """A queued job, by id, so the caller can tell slow from failed."""

    job_id: uuid.UUID


class JobStatusOut(BaseModel):
    id: uuid.UUID
    type: JobType
    status: JobStatus
    # On FAILED, verbatim from the worker.
    error: str | None = None
    attempts: int
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class CollaboratorOut(BaseModel):
    user_id: uuid.UUID
    email: str | None
    display_name: str | None
    level: CollaboratorLevel
    created_at: datetime


class CollaboratorAdd(BaseModel):
    # Not EmailStr, which needs the email-validator package.
    email: str = Field(min_length=3, max_length=320)
    level: CollaboratorLevel


class CollaboratorUpdate(BaseModel):
    level: CollaboratorLevel
