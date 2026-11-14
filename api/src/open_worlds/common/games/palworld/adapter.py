"""Palworld: one UDP port per world, no version manifest, no content registry."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from ... import world_fs
from ...config import get_settings
from ...logging import get_logger
from ..base import (
    AdminClient,
    AdminUnavailable,
    GameAdapter,
    ImportedSettings,
    ImportPolicy,
    PlayerInfo,
)
from . import settings_file
from .config import PalworldConfig, palworld_config
from .restapi import PalApiClient, PalApiError
from .settings import PalworldSettings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...orchestrator import WorldSpec


#: The world's settings file, relative to its data directory.
SETTINGS_RELPATH = "Pal/Saved/Config/LinuxServer/PalWorldSettings.ini"

#: Pinned so an uploaded settings file cannot move the RCON listener (RCON
#: itself stays disabled).
RCON_PORT = 25575

#: The stock ban list, pinned: see settings_overrides().
BAN_LIST_URL = "https://b.palworldgame.com/api/banlist.txt"

#: Where the dedicated server looks for saves; one child per world. "0" is the
#: local-user slot it always uses.
SAVEGAMES_RELPATH = "Pal/Saved/SaveGames/0"

#: Holds DedicatedServerName. Mostly client settings, so the platform owns
#: exactly that one key.
GAME_USER_SETTINGS_RELPATH = "Pal/Saved/Config/LinuxServer/GameUserSettings.ini"

#: What the image checks to decide the server is installed, relative to the
#: data directory (helper_install.sh, IsInstalled).
INSTALLED_FILES = ("PalServer.sh", "steamapps/appmanifest_2394010.acf")

_LOCAL_SETTINGS_SECTION = "[/Script/Pal.PalGameLocalSettings]"
_DEDICATED_SERVER_NAME = "DedicatedServerName"


def _write_dedicated_server_name(data_dir: str, world: str) -> bool:
    """Point the config at ``world``. Returns whether anything changed.

    Line-oriented, touching only that line: the file belongs to the game.
    Written through world_fs: the world owns every directory on the path.
    """
    line = f"{_DEDICATED_SERVER_NAME}={world}"
    existing = world_fs.read_text(data_dir, GAME_USER_SETTINGS_RELPATH)
    if existing is None:
        world_fs.write_text(
            data_dir, GAME_USER_SETTINGS_RELPATH, f"{_LOCAL_SETTINGS_SECTION}\n{line}\n"
        )
        return True

    lines = existing.splitlines()
    for i, current in enumerate(lines):
        if current.strip().startswith(f"{_DEDICATED_SERVER_NAME}="):
            if current.strip() == line:
                return False
            lines[i] = line
            world_fs.write_text(data_dir, GAME_USER_SETTINGS_RELPATH, "\n".join(lines) + "\n")
            return True

    # Key absent: put it under its own section, or start that section.
    if _LOCAL_SETTINGS_SECTION in (stripped := [line_.strip() for line_ in lines]):
        lines.insert(stripped.index(_LOCAL_SETTINGS_SECTION) + 1, line)
    else:
        lines = [_LOCAL_SETTINGS_SECTION, line, *lines]
    world_fs.write_text(data_dir, GAME_USER_SETTINGS_RELPATH, "\n".join(lines) + "\n")
    return True


# The save lives under Pal/Saved/; the rest is a ~10 GB SteamCMD install the
# image reinstalls. Keep the save tree, drop the game.
PALWORLD_IMPORT_POLICY = ImportPolicy(
    known_root_entries=frozenset(
        {
            # The container's data root; only Saved/ survives (partial_subtrees).
            "pal",
            # Archives are often rooted deeper.
            "saved",
            "savegames",
            # Server config, when archived beside the save.
            "palworldsettings.ini",
            "gameusersettings.ini",
        }
    ),
    world_child_markers=frozenset(
        {"level.sav", "levelmeta.sav", "worldoption.sav", "players", "savegames", "saved"}
    ),
    data_root_markers=frozenset({"pal", "saved", "savegames", "level.sav"}),
    # Unreal content packages (how Palworld mods ship), native libraries and
    # scripts: the server would load or run any of them.
    executable_suffixes=(
        ".pak",
        ".ucas",
        ".utoc",
        ".dll",
        ".so",
        ".exe",
        ".sh",
        ".bat",
        ".cmd",
        ".lua",
    ),
    bare_world_marker="level.sav",
    backup_exclude_dirs=frozenset(
        # linux64 is the Steam runtime: its contents are stripped as code, and
        # the empty directory left behind was offered to the owner for pruning.
        {"steamapps", "steam", ".steam", "steamcmd", "engine", "logs", "backups", "linux64"}
    ),
    # Unreal's manifests of the shipped (reinstallable) game files.
    backup_exclude_files=frozenset(
        {
            "palworldsettings.ini.bak",
            # The install's settings template, not the world's settings.
            "defaultpalworldsettings.ini",
            "manifest_ufsfiles_linux.txt",
            "manifest_nonufsfiles_linux.txt",
            "manifest_debugfiles_linux.txt",
        }
    ),
    # Inside Pal/ only the save tree survives.
    partial_subtrees={"pal": "saved"},
    placements=(("Pal", ""), ("Saved", "Pal"), ("SaveGames", "Pal/Saved")),
    # The descent to the data root consumed the save folder's own name (its
    # GUID), so it needs a new one.
    bare_world_placement="Pal/Saved/SaveGames/0/imported",
    # The server picks its world by DedicatedServerName; more than one save
    # directory would be ambiguous.
    enforce_single_world=True,
    # Small, fixed layout and no mods, so name the survivors. This matters
    # beyond the game: the image skips SteamCMD and runs whatever binaries are
    # on disk if PalServer.sh and the app manifest exist, so an upload must not
    # be able to land them.
    allowed_paths=(
        # The slot is the game's to choose, so it is matched.
        "Pal/Saved/SaveGames/*/*/Level.sav",
        "Pal/Saved/SaveGames/*/*/LevelMeta.sav",
        # Includes <uid>_dps.sav (Dimensional Pal Storage), which appears only
        # once someone has used it.
        "Pal/Saved/SaveGames/*/*/Players/*.sav",
        # Rendered by the platform; the source of truth for world rules.
        "Pal/Saved/Config/*/PalWorldSettings.ini",
        # Names the save directory above.
        "Pal/Saved/Config/*/GameUserSettings.ini",
        # Never regenerated (the image sets DISABLE_GENERATE_ENGINE). Config/*
        # rather than LinuxServer, so a world once hosted on Windows keeps its
        # settings.
        "Pal/Saved/Config/*/Engine.ini",
        # The platform's world icon; the game never reads it.
        "Pal/Saved/open-worlds-icon.png",
        # The same icon under the platform's earlier name.
        "Pal/Saved/gameplatform-icon.png",
    ),
    icon_relpath="Pal/Saved/open-worlds-icon.png",
    legacy_icon_relpaths=("Pal/Saved/gameplatform-icon.png",),
)


log = get_logger(__name__)


class _RestAdminClient(AdminClient):
    """Adapts Palworld's REST admin API to the platform's admin interface."""

    def __init__(self, host: str, port: int, password: str) -> None:
        self._api = PalApiClient(host, port, password)

    def save_flush(self) -> None:
        # One synchronous save and no save-off to bracket the archive with.
        # Saves are written atomically per file, so at worst a backup misses
        # the last few seconds.
        try:
            self._api.save()
        except PalApiError as exc:
            raise AdminUnavailable(str(exc)) from exc

    def save_resume(self) -> None:
        """No-op: nothing was suspended, because there is no save-off to undo."""

    def players(self) -> list[PlayerInfo]:
        try:
            return [PlayerInfo(name=p.name, player_id=p.player_id) for p in self._api.players()]
        except PalApiError as exc:
            raise AdminUnavailable(str(exc)) from exc

    def server_version(self) -> str | None:
        # The liveness probe's call; the version rides along.
        try:
            info = self._api.info()
        except PalApiError as exc:
            raise AdminUnavailable(str(exc)) from exc
        v = info.get("version")
        if not v:
            # Upstream could rename the key; say what we got instead.
            log.warning(
                "server info had no version field",
                extra={"keys": sorted(info)},
            )
            return None
        return str(v).strip() or None

    def close(self) -> None:
        """Stateless over HTTP; nothing to release."""


class PalworldAdapter(GameAdapter):
    game = "PALWORLD"
    display_name = "Palworld"
    # By digest: a guessed tag once failed only at world start. This is
    # v2.8.1's multi-arch index (the first with SETUP_ONLY); the image versions
    # its own releases, not the game's, so read its release notes before
    # bumping. Dependabot cannot see a pin in Python; scripts/game-images.sh
    # --check (weekly in CI) reports a newer release.
    image = (
        "thijsvanloef/palworld-server-docker@sha256:"
        "86fc7470949f6fcc931cb6443f61facc79bacc09e3f40d46e628d9e1ef6e56cd"
    )
    data_mount = "/palworld"
    container_caps = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "SETGID", "SETUID", "KILL")
    # No hostname in the protocol, so the port is the routing key.
    allocates_port = True
    container_port = 8211
    # Its shutdown saves and cleans up, and outran a 60s window. Too long costs
    # a slower stop; too short kills a save.
    stop_timeout_seconds = 300
    # The image's start.sh always exits 0, even after a segfault, so only an
    # exit nobody asked for reveals a crash.
    stops_only_via_platform = True
    # The install step pulls ~4.5 GB on first boot and after every restore,
    # and counts toward the boot.
    readiness_timeout_seconds = 1800
    # The image's own give-up line, and Unreal's crash banner.
    fatal_log_patterns = (
        "Server Not Installed Properly",
        "Fatal error!",
    )
    # Only a cgroup ceiling the server grows into, so headroom is free until
    # used. Hitting it kills the server mid-session.
    default_memory_mb = 8192
    # A Steam app updated in place: nothing to pin.
    supports_version_pinning = False
    # No trusted source of mods to curate.
    supports_content_catalog = False
    # Its only access control: COMMUNITY=false unlists a world, but anyone
    # with the address can still join.
    supports_server_password = True
    # Not display_name: the server name comes from the immutable subdomain.
    # server_description is: a running server never re-reads its settings.
    spec_fields = ("memory_mb", "max_players", "server_password", "server_description")
    import_policy = PALWORLD_IMPORT_POLICY
    settings_model = PalworldSettings
    edge_profile = "palworld"

    def config(self) -> PalworldConfig:
        return palworld_config()

    def environment(self, spec: WorldSpec) -> dict[str, str]:
        s = get_settings()
        return {
            # Always the stock port; the host port is pal-waker's.
            "PORT": str(self.container_port),
            # We render PalWorldSettings.ini (prepare_data_dir) and it travels
            # with the save, so stop the image regenerating it from env.
            "DISABLE_GENERATE_SETTINGS": "true",
            # Also read from the environment by the image's own scripts, so these
            # must match the ini that settings_overrides() writes.
            "ADMIN_PASSWORD": spec.admin_password,
            "REST_API_ENABLED": "true",
            "REST_API_PORT": str(palworld_config().rest_api_port),
            # Deprecated upstream in favour of the REST API.
            "RCON_ENABLED": "false",
            # Unlisted: worlds are reached by address.
            "COMMUNITY": "false",
            # The platform owns backups.
            "BACKUP_ENABLED": "false",
            # With no version manifest, an update on restart is the one thing
            # that could change a world's game version under its save.
            "UPDATE_ON_BOOT": "false",
            # On by default since v2.8.0, which fetches and loads UE4SS and
            # PalSchema on every boot. The platform offers no mods.
            "MOD_ENABLED": "false",
            # Pocketpair's recommended performance flags; without them the
            # simulation ran on one thread. WORKER_THREADS_SERVER stays unset:
            # the image's fallback counts host cores, ignoring the CPU cap.
            "ENABLE_PERF_THREADING_ARGS": "true",
            # See PalworldConfig.palworld_allow_negative_delta_time.
            "PALWORLD_ALLOW_NEGATIVE_DELTA_TIME": (
                "true" if palworld_config().palworld_allow_negative_delta_time else "false"
            ),
            "PUID": str(s.run_uid),
            "PGID": str(s.run_gid),
        }

    def install_needed(self, spec: WorldSpec, data_dir: str) -> bool:
        # The image's own test (IsInstalled): without both files its start
        # runs SteamCMD, which the play list cannot reach.
        return not all(
            os.path.isfile(os.path.join(data_dir, *rel.split("/"))) for rel in INSTALLED_FILES
        )

    def install_environment(self, spec: WorldSpec) -> dict[str, str]:
        # Install, write the config, exit 0; the server never starts.
        return {**self.environment(spec), "SETUP_ONLY": "true"}

    def settings_overrides(self, spec: WorldSpec) -> dict[str, str]:
        """What the platform writes over a world's own settings file.

        The owner's choices from the database, then keys where an untrusted
        uploaded file winning would break the platform or weaken security.
        Every other key passes through untouched.
        """
        return {
            # -- The owner's, from the database ------------------------------
            "ServerDescription": settings_file.quote(spec.settings.get("server_description", "")),
            # Empty means open.
            "ServerPassword": settings_file.quote(spec.server_password),
            "ServerPlayerMaxNum": str(spec.max_players),
            # -- Forced: platform correctness --------------------------------
            # The immutable subdomain, so a rename needs no restart.
            "ServerName": settings_file.quote(spec.subdomain),
            # Must match environment(), which the image's scripts read.
            "AdminPassword": settings_file.quote(spec.admin_password),
            "RESTAPIEnabled": "True",
            "RESTAPIPort": str(palworld_config().rest_api_port),
            "RCONEnabled": "False",
            # An imported file may carry another host's port or address; the
            # waker routes only the stock port.
            "PublicPort": str(self.container_port),
            "PublicIP": settings_file.quote(""),
            "RCONPort": str(RCON_PORT),
            # -- Forced: security posture ------------------------------------
            # False would disable player authentication.
            "bUseAuth": "True",
            # Fetched by the server: a user's value would be an arbitrary
            # outbound request, and let a third party decide who may join.
            "BanListURL": settings_file.quote(BAN_LIST_URL),
            # The image's default. False would break client mods for existing
            # worlds; a separate decision.
            "bAllowClientMod": "True",
        }

    def settings_from_archive(self, archive_path: str) -> ImportedSettings | None:
        from ...upload_inspect import read_member_text

        try:
            text = read_member_text(archive_path, os.path.basename(SETTINGS_RELPATH))
        except ValueError:
            return None
        if not text or not text.strip():
            return None
        try:
            values = settings_file.parse(text)
        except settings_file.SettingsError:
            # Still worth importing; it starts from our defaults.
            log.info("uploaded settings file could not be parsed", extra={"path": archive_path})
            return None

        max_players: int | None = None
        raw = values.get("ServerPlayerMaxNum")
        if raw is not None:
            try:
                max_players = int(settings_file.unquote(raw))
            except ValueError:
                max_players = None
        return ImportedSettings(
            values=values,
            max_players=max_players,
            server_password=settings_file.unquote(values.get("ServerPassword")),
            settings=_adoptable_settings(values),
        )

    def _world_dirs(self, data_dir: str) -> list[str]:
        """Save directories under SaveGames/0, by name. A world is a directory
        holding Level.sav, the import policy's marker too."""
        root = os.path.join(data_dir, *SAVEGAMES_RELPATH.split("/"))
        if not os.path.isdir(root):
            return []
        return sorted(
            name
            for name in os.listdir(root)
            if os.path.isfile(os.path.join(root, name, "Level.sav"))
        )

    def _point_at_world(self, spec: WorldSpec, data_dir: str) -> None:
        """Point DedicatedServerName at the save directory actually on disk.

        The directory is the truth. A name matching nothing makes the server
        silently create an empty world beside the real save.
        """
        worlds = self._world_dirs(data_dir)

        if not worlds:
            # Never booted: the game names it itself.
            return

        if len(worlds) > 1:
            # Uploads are held to one save directory, so two is a broken
            # invariant. Starting anyway would hide which one is being played.
            raise RuntimeError(
                f"{spec.subdomain} has {len(worlds)} save directories "
                f"({', '.join(worlds)}); exactly one is required. "
                f"Remove the ones that are not in use and start the world again."
            )

        if _write_dedicated_server_name(data_dir, worlds[0]):
            log.info(
                "pointed DedicatedServerName at the save directory on disk",
                extra={"subdomain": spec.subdomain, "world": worlds[0]},
            )

    def prepare_data_dir(self, spec: WorldSpec, data_dir: str) -> None:
        """Render the config files before the server reads them. Every start:
        a running server never re-reads them."""
        self._point_at_world(spec, data_dir)

        existing = world_fs.read_text(data_dir, SETTINGS_RELPATH)

        text, lowered = settings_file.render(existing, self.settings_overrides(spec))
        if lowered:
            log.info(
                "clamped world settings over the configured ceiling",
                extra={"subdomain": spec.subdomain, "settings": sorted(lowered)},
            )

        # The image's entrypoint fixes ownership (it holds CHOWN/FOWNER).
        world_fs.write_text(data_dir, SETTINGS_RELPATH, text)

    def labels(self, spec: WorldSpec) -> dict[str, str]:
        # The host port pal-waker relays to this container.
        ns = get_settings().label_namespace
        return {f"{ns}.game_port": str(spec.game_port or 0)}

    def admin_client(self, host: str, password: str) -> AdminClient:
        return _RestAdminClient(host, palworld_config().rest_api_port, password)

    def connect_address(self, subdomain: str, domain: str, game_port: int | None) -> str:
        # The port is what tells one world from another.
        return f"{subdomain}.{domain}:{game_port}" if game_port else f"{subdomain}.{domain}"


def _adoptable_settings(values: dict[str, str]) -> dict[str, str]:
    """The world settings an uploaded file carries that the platform can keep."""
    description = (settings_file.unquote(values.get("ServerDescription")) or "").strip()
    try:
        PalworldSettings(server_description=description)
    except ValueError:
        # Characters the file cannot hold: leave it unset, don't fail.
        return {}
    return {"server_description": description} if description else {}
