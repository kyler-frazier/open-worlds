"""The game seam: everything that differs between games, declared on a GameAdapter.

The rest of the platform (auth, jobs, backups, reconcile, container hardening)
is written once against the adapter and never asks which game it is. A change
that needs an ``if game == ...`` outside ``games/`` has found a missing hook.
See docs/adding-a-game.md.
"""

from __future__ import annotations

import abc
import fnmatch
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import APIRouter
    from sqlalchemy.orm import Session

    from ..models import Job, World
    from ..orchestrator import WorldSpec


#: What a game's card artwork may be.
ART_EXTENSIONS = (".webp", ".png", ".jpg", ".jpeg", ".avif")


class GameSettingsError(ValueError):
    """A world's game settings were refused. The message is meant for the user."""


class InstallIncomplete(RuntimeError):
    """The install step exited cleanly but left the server unable to start
    without the network, so the world must not move on to its play stage."""


class GameSettingsConflict(GameSettingsError):
    """Refused because of the world's other state, not the settings themselves."""


class NoSettings(BaseModel):
    """Settings for a game whose worlds have none of their own."""

    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class PeriodicJob:
    """Work a game needs done on a timer by the worker, whatever the world."""

    #: Scheduler id; unique across games.
    id: str
    seconds: int
    #: Called with no arguments; opens its own database session.
    run: Callable[[], None]


@dataclass(frozen=True)
class PlayerInfo:
    """One connected player, normalised across the games' admin protocols."""

    name: str
    # Opaque per-game identifier; not comparable across games.
    player_id: str = ""


class AdminClient(Protocol):
    """What the platform needs from a running game server: flush a save, list
    players, report a version."""

    def save_flush(self) -> None:
        """Ask the server to write the world to disk before we archive it."""

    def save_resume(self) -> None:
        """Undo whatever :meth:`save_flush` suspended, once the archive is done.

        Always paired with ``save_flush``; a no-op where nothing was suspended
        (Palworld has only a one-shot save).
        """

    def players(self) -> list[PlayerInfo]:
        """Currently connected players (empty when nobody is on)."""

    def server_version(self) -> str | None:
        """The version actually running, or None if the server cannot say."""
        return None

    def close(self) -> None:
        """Release any connection held. Safe to call more than once."""


class AdminUnavailable(Exception):
    """The server's admin interface could not be reached or refused us.

    Callers carry on without it (a cold backup, the previous player count), so
    it must never escape as a hard failure.
    """


@dataclass(frozen=True)
class ImportedSettings:
    """Server settings carried by an uploaded archive.

    Read at preflight to show the owner, and again at commit to seed the world.
    The archive is the only copy; nothing is stored between the two.
    """

    #: Every setting found, for display. Raw values, as written in the file.
    values: dict[str, str]
    #: The subset the platform owns, normalised. None means the file did not say.
    max_players: int | None = None
    server_password: str | None = None
    #: World settings (the adapter's ``settings_model`` fields) the file sets.
    #: Adopted on import only where the world has no value of its own.
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ImportPolicy:
    """What survives an upload, and where the survivors go.

    Uploads are untrusted. A world is its save; everything else in a server
    directory is regenerable or executable, so the keep-list is narrow and the
    executable strip is absolute.
    """

    # Top-level entries at the data root that are kept by name (lowercased).
    known_root_entries: frozenset[str]
    # Children that identify a directory as a world by content, so custom-named
    # worlds are recognised.
    world_child_markers: frozenset[str]
    # Names that mark the server data root, where descent through
    # single-directory archive wrappers stops.
    data_root_markers: frozenset[str]
    # Suffixes treated as untrusted loadable/executable code, wherever they sit.
    executable_suffixes: tuple[str, ...]
    # A data root holding this file is itself one world, so its contents are
    # world internals rather than prunable extras.
    bare_world_marker: str
    # Top-level entries excluded from backups because the image regenerates them.
    backup_exclude_dirs: frozenset[str]
    backup_exclude_files: frozenset[str] = frozenset()
    # Subtrees kept only in part: {"pal": "saved"} keeps Pal/Saved/ and prunes
    # the rest of Pal/.
    partial_subtrees: dict[str, str] = field(default_factory=dict)
    # Where an archive's contents go under the data dir, keyed by the outermost
    # directory the upload starts at. Checked in order.
    placements: tuple[tuple[str, str], ...] = ()
    # Placement for a bare world folder whose own name was consumed by the
    # descent to the data root. Empty means the data root.
    bare_world_placement: str = ""
    # Paths that survive an import, as case-insensitive patterns relative to the
    # data dir. ``*`` never crosses ``/``, so a pattern's depth is exact.
    #
    # Empty keeps whatever the deny rules did not strip: right for an open-ended
    # layout (Minecraft modpacks invent directories). Where the layout is small
    # and fixed, naming the survivors is stronger, since a deny-list keeps
    # anything nobody thought to forbid.
    allowed_paths: tuple[str, ...] = ()
    # At most one directory holding ``bare_world_marker`` per upload. Minecraft
    # must leave it off (world/, world_nether/ and world_the_end/ each have a
    # level.dat); Palworld needs it to know which save to point the server at.
    enforce_single_world: bool = False
    # The world icon's exact path relative to the data dir, or None. Inside the
    # save tree so backups carry it; common/world_icon.py checks its shape.
    icon_relpath: str | None = None
    # Older paths of the same icon that archives may still carry. Allowed on
    # import, then moved to icon_relpath (world_icon.adopt_legacy_icons).
    legacy_icon_relpaths: tuple[str, ...] = ()
    # The save-folder icon the game falls back to when icon_relpath is absent
    # (Minecraft: <level-name>/icon.png). Read, never written.
    level_icon_name: str | None = None

    def path_allowed(self, rel_posix: str) -> bool:
        """Whether a path under the data dir survives. True when no paths are named."""
        if not self.allowed_paths:
            return True
        return self._matches_allowed(rel_posix, prefix=False)

    def dir_may_hold_allowed(self, rel_posix: str) -> bool:
        """Whether a directory could contain a path ``path_allowed`` keeps, so a
        walk can prune whole subtrees. True when no paths are named."""
        if not self.allowed_paths:
            return True
        return self._matches_allowed(rel_posix, prefix=True)

    def _matches_allowed(self, rel_posix: str, *, prefix: bool) -> bool:
        parts = [p.lower() for p in rel_posix.split("/") if p]
        for pattern in self.allowed_paths:
            pat_parts = [p.lower() for p in pattern.split("/") if p]
            if prefix:
                if len(parts) >= len(pat_parts):
                    continue
                pat_parts = pat_parts[: len(parts)]
            elif len(parts) != len(pat_parts):
                continue
            if all(
                fnmatch.fnmatchcase(part, pat) for part, pat in zip(parts, pat_parts, strict=True)
            ):
                return True
        return False

    def is_prunable_nested(self, rel_posix: str) -> bool:
        """Whether a path inside a kept directory must still be stripped: it is
        loadable code, or in a partially kept subtree's discarded half."""
        parts = [p.lower() for p in rel_posix.split("/") if p]
        if not parts:
            return False
        if parts[-1].endswith(self.executable_suffixes):
            return True
        keep = self.partial_subtrees.get(parts[0])
        if keep is not None and (len(parts) < 2 or parts[1] != keep):
            return True
        return False

    def placement_for(self, entries: list[str]) -> str:
        """Path under the data dir where an extracted archive's contents belong."""
        lowered = {e.lower() for e in entries}
        for name, dest in self.placements:
            if name.lower() in lowered:
                return dest
        if self.bare_world_marker.lower() in lowered:
            return self.bare_world_placement
        return ""


class GameAdapter(abc.ABC):
    """Everything the platform needs to know about one game."""

    #: Stored on every world and used as the container's game label, so it
    #: never changes once a world exists. Upper case (``MINECRAFT``).
    game: str
    display_name: str
    #: Pinned by digest in production.
    image: str
    #: Where the world's data directory mounts inside the container.
    data_mount: str
    #: Capabilities the entrypoint needs before dropping root. All others are
    #: dropped.
    container_caps: tuple[str, ...]
    #: True when the protocol gives a proxy no way to tell worlds apart on one
    #: port (Palworld's UDP), so each world needs its own host port.
    allocates_port: bool
    container_port: int
    #: Grace before Docker escalates to SIGKILL. Too short kills the server
    #: mid-save on every idle sleep.
    stop_timeout_seconds: int
    #: Whether every legitimate stop goes through the platform (Palworld: the
    #: waker asks). If so, a container found exited while its world reads
    #: RUNNING crashed, whatever its exit code says.
    stops_only_via_platform: bool
    #: How long a world may be running but silent before it stops counting as
    #: STARTING. Palworld installs gigabytes via SteamCMD on first start and
    #: after every restore, so this differs by an order of magnitude.
    readiness_timeout_seconds: int
    #: Log lines that mean the server failed to start even if its process lives.
    #: Seen during boot, the world is stopped and marked ERROR
    #: (common/startup_errors.py). A false match stops a healthy world.
    fatal_log_patterns: tuple[str, ...] = ()
    #: MiB a new world gets when its owner names no figure. Not comparable
    #: across games: Minecraft's heap is reserved at boot, Palworld's is only a
    #: ceiling. Clamped to the operator's bounds; ``PLATFORM_DEFAULT_MEMORY_MB``
    #: overrides it.
    default_memory_mb: int
    supports_version_pinning: bool
    #: Whether admin-curated installable content exists for this game.
    supports_content_catalog: bool
    #: Whether the server has a shared join password. Minecraft does not: its
    #: whitelist of authenticated names is a different mechanism.
    supports_server_password: bool
    #: Fields that only apply when the container is recreated, so they change
    #: only while the world is OFF. World columns (``memory_mb``) and settings
    #: keys (``version``) share this namespace.
    spec_fields: tuple[str, ...]
    import_policy: ImportPolicy
    #: Offered on a fresh platform before an admin chooses (Minecraft only).
    enabled_by_default: ClassVar[bool] = False
    #: The compose profile that runs this game's router, if any. The game can
    #: only be enabled while it is active on the host.
    edge_profile: ClassVar[str | None] = None
    #: The owner's choices only this game has, stored as JSON on the world.
    #: Always ``extra="forbid"``: a kept unknown key would promise a setting
    #: that does nothing.
    settings_model: ClassVar[type[BaseModel]] = NoSettings

    # --- World settings -----------------------------------------------------

    def new_settings(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Validated settings for a new world, with the game's defaults filled in.

        Raises GameSettingsError with a message for the user.
        """
        return self.validate_settings(raw)

    def updated_settings(self, current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        """``current`` with ``patch`` applied, validated as a whole."""
        return self.validate_settings({**current, **patch})

    def validate_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.settings_model.model_validate(data).model_dump(mode="json")
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first.get("loc", ()))
            msg = str(first.get("msg", "invalid")).removeprefix("Value error, ")
            raise GameSettingsError(f"{where}: {msg}" if where else msg) from exc

    def settings_risks(self, current: dict[str, Any], new: dict[str, Any]) -> list[str]:
        """Consequences of a settings change the owner must confirm, in words."""
        return []

    def check_settings_change(self, db: Session, world: World, new: dict[str, Any]) -> None:
        """Raise GameSettingsError for a change the world's other state cannot
        survive (Minecraft: a loader switch that strands installed mods)."""
        return None

    def settings_schema(self) -> dict[str, Any]:
        """JSON schema of ``settings_model`` for the UI's form, with defaults the
        game decides at runtime filled in (Minecraft's latest release)."""
        return self.settings_model.model_json_schema()

    def egress_variant(self, settings: dict[str, Any]) -> str:
        """Which of the game's egress allow-list entries apply to this world
        (Minecraft: its loader). Empty means all of them."""
        return ""

    def art_path(self) -> str | None:
        """This game's card artwork on disk, if the operator supplied one.

        ``PLATFORM_GAME_ART_<GAME>`` names a file (absolute, or relative to
        ``PLATFORM_GAME_ART_DIR``); otherwise ``<game>.<ext>`` in that directory.
        Images only, so a typo cannot serve some other file.
        """
        import os

        from ..config import get_settings  # local: config is imported by adapters

        base = get_settings().game_art_dir
        override = os.environ.get(f"PLATFORM_GAME_ART_{self.game}", "").strip()
        if override:
            candidates = [os.path.join(base, override) if base else override]
        elif base:
            stem = os.path.join(base, self.game.lower())
            candidates = [stem + ext for ext in ART_EXTENSIONS]
        else:
            return None
        for path in candidates:
            if path.lower().endswith(ART_EXTENSIONS) and os.path.isfile(path):
                return path
        return None

    def enable_problems(self) -> list[str]:
        """What this host lacks for the game to be offered; empty when nothing.

        Default: its router's compose profile must be running.
        """
        from ..config import get_settings  # local: config is imported by adapters

        if self.edge_profile and self.edge_profile not in get_settings().profiles:
            return [
                f"Its router isn't running on this host: add {self.edge_profile} to "
                f"COMPOSE_PROFILES in deploy/.env and run make up."
            ]
        return []

    # --- What a game adds to the platform -----------------------------------

    def config(self) -> BaseModel | None:
        """This game's own ``PLATFORM_*`` settings, or None if it has none."""
        return None

    def periodic_jobs(self) -> list[PeriodicJob]:
        """Worker jobs this game needs, scheduled while the game is enabled."""
        return []

    def api_routers(self) -> list[APIRouter]:
        """Extra API routes under /api, mounted even while the game is disabled
        so existing worlds' routes refuse rather than 404."""
        return []

    def job_handlers(self) -> dict[str, Callable[[Session, Job], None]]:
        """Handlers for queued job types this game owns, by JobType value."""
        return {}

    def spec_extras(self, db: Session, world: World) -> dict[str, Any]:
        """JSON-safe runtime facts for the container spec that are not settings
        (Minecraft: the installed resource pack)."""
        return {}

    def managed_content_hashes(self, db: Session, world: World) -> set[str]:
        """sha512s of files the platform installed into the world. Backups leave
        them out; restores reinstall them from the catalog."""
        return set()

    def after_restore(self, db: Session, world: World) -> None:
        """Put back whatever ``managed_content_hashes`` left out of the archive."""
        return None

    @abc.abstractmethod
    def environment(self, spec: WorldSpec) -> dict[str, str]:
        """Environment for a world container, built from its database record."""

    # --- Install and play ---------------------------------------------------
    # A world downloads its server in a separate install container, behind the
    # game's INSTALL egress list, then runs behind the PLAY list, which has no
    # download hosts. So a mod cannot reach what the install needed, and the
    # install never runs the world's own code.

    #: Directories under ``data_mount`` replaced by an empty tmpfs in the
    #: install container: where the world's own code lives (Minecraft's mods
    #: and plugins), which some installs launch the server far enough to load.
    install_hidden_paths: tuple[str, ...] = ()

    def install_needed(self, spec: WorldSpec, data_dir: str) -> bool:
        """Whether the server must be installed before it can run offline: its
        files are missing, or no longer match the world. Default: never, so the
        game's PLAY list must carry whatever the server downloads."""
        return False

    def install_environment(self, spec: WorldSpec) -> dict[str, str]:
        """Environment for the install container: the image's own install-only
        mode, which exits 0 when the server is ready to run offline."""
        raise NotImplementedError(f"{self.game} has no install step")

    def finish_install(self, spec: WorldSpec, data_dir: str) -> None:
        """Called after the install container exited 0, on the host. Record
        what the play stage needs; raise InstallIncomplete if it is missing."""
        if self.install_needed(spec, data_dir):
            raise InstallIncomplete(f"the {self.display_name} install exited without the server")

    def run_environment(self, spec: WorldSpec, data_dir: str) -> dict[str, str]:
        """Environment for the server itself, once installed: run what is on
        disk rather than download it."""
        return self.environment(spec)

    @abc.abstractmethod
    def admin_client(self, host: str, password: str) -> AdminClient:
        """Client for a running world's admin interface, on the private network."""

    @abc.abstractmethod
    def connect_address(self, subdomain: str, domain: str, game_port: int | None) -> str:
        """The address a player types, as shown in the dashboard."""

    def labels(self, spec: WorldSpec) -> dict[str, str]:
        """Game-specific container labels, merged with the platform's own."""
        return {}

    def prepare_data_dir(self, spec: WorldSpec, data_dir: str) -> None:
        """Write what the server must find before it starts. Runs on the host on
        every start, so a bad config is refused before the server loads it
        (Palworld writes PalWorldSettings.ini here)."""
        return None

    def settings_from_archive(self, archive_path: str) -> ImportedSettings | None:
        """Settings an uploaded archive carries, or None. Must not raise on a
        malformed config: that is something to report, not a failed upload."""
        return None
