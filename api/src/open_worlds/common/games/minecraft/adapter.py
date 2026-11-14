"""Minecraft: hostname-routed worlds sharing one port, with a curated catalog."""

from __future__ import annotations

import json
import os
import re
import time
from typing import TYPE_CHECKING, Any

from ... import world_fs
from ...config import get_settings
from ..base import (
    AdminClient,
    AdminUnavailable,
    GameAdapter,
    GameSettingsConflict,
    GameSettingsError,
    ImportPolicy,
    InstallIncomplete,
    PeriodicJob,
    PlayerInfo,
)
from .config import MinecraftConfig, minecraft_config
from .rcon import RconClient, RconError
from .settings import VERSION_HELP, MinecraftSettings, ServerType
from .versions import latest_release, release_tuple, version_exists

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from fastapi import APIRouter
    from sqlalchemy.orm import Session

    from ...models import Job, World
    from ...orchestrator import WorldSpec

# Share of a world's memory given to the JVM heap. The rest covers what the
# heap does not (metaspace, stacks, direct buffers); heap equal to the limit
# got busy worlds OOM-killed. The itzg image's own guidance.
JVM_HEAP_FRACTION = 0.8
# Vanilla does not start with less.
_MIN_JVM_HEAP_MB = 512


def jvm_heap_mb(memory_mb: int) -> int:
    """The JVM heap for a world whose container is capped at ``memory_mb``."""
    return max(_MIN_JVM_HEAP_MB, int(memory_mb * JVM_HEAP_FRACTION))


# --- Install ------------------------------------------------------------------
# The image's normal start resolves and downloads the server on every boot, so
# it cannot run behind the play list. The install container does that once;
# the server then runs as TYPE=CUSTOM from the files on disk.

#: The platform's record of a finished install, in the world's data dir. The
#: world can write it too; at worst that forces an install (which runs only
#: the image's and the vendor's code) or skips one (and the server, unable to
#: download, fails to start).
INSTALL_MARKER = ".open-worlds-install.json"

#: Reinstalled after this long, so Paper and Fabric builds and the Forge
#: installer stay current: a server behind the play list cannot update itself.
INSTALL_MAX_AGE_SECONDS = 7 * 24 * 3600

#: Where the image records each loader's server; vanilla's name is fixed.
_INSTALL_RESULTS = {
    "PAPER": ".paper.env",
    "FABRIC": ".install-fabric.env",
    "FORGE": ".run-forge.env",
}

#: What a loader's first launch fetches, so an install that left one out (or a
#: restore that dropped it) means installing again.
_INSTALL_OUTPUTS = ("libraries", "versions", ".fabric", "cache")

#: The family each type runs as. TYPE=CUSTOM would otherwise default to HYBRID.
_FAMILY = {"VANILLA": "VANILLA", "PAPER": "SPIGOT", "FABRIC": "FABRIC", "FORGE": "FORGE"}

_SERVER_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\.(?:jar|sh)")


def _read_results(path: str) -> dict[str, str]:
    """``KEY="value"`` lines, as the image writes them."""
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                key, sep, value = line.strip().partition("=")
                if sep:
                    out[key] = value.strip().strip('"')
    except OSError:
        pass
    return out


# Top-level entries kept by name: the world and server state, and modpack
# configs. World folders are also recognised by content. Everything else
# (caches, logs, loose jars, kubejs/) is prunable. mods/ and plugins/ survive as
# directories, but the jars in them are stripped.
#
# Datapacks pass through, .mcfunction included, on purpose. They run only game
# commands (no filesystem, process or network), an upload can already name the
# operators in ops.json, and the container bounds resource abuse. Many worlds
# are built around one. Do not add .mcfunction to executable_suffixes.
_KNOWN_ROOTS = frozenset(
    {
        # The world, and server state and config.
        "world",
        "world_nether",
        "world_the_end",
        "server.properties",
        "ops.json",
        "whitelist.json",
        "white-list.txt",
        "banned-players.json",
        "banned-ips.json",
        "banned-players.txt",
        "banned-ips.txt",
        "usercache.json",
        "server-icon.png",
        # Bukkit/Spigot/Paper config: declarative.
        "help.yml",
        "commands.yml",
        "permissions.yml",
        "wepif.yml",
        "bukkit.yml",
        "spigot.yml",
        "paper.yml",
        "paper-global.yml",
        "paper-world-defaults.yml",
        "datapacks",
        # Kept, but the jars inside are stripped.
        "mods",
        "plugins",
        # Modpack configs.
        "config",
        "defaultconfigs",
    }
)

MINECRAFT_IMPORT_POLICY = ImportPolicy(
    known_root_entries=_KNOWN_ROOTS,
    # The order the server reads them in: server-icon.png, then the save's
    # icon.png (singleplayer writes one).
    icon_relpath="server-icon.png",
    level_icon_name="icon.png",
    world_child_markers=frozenset(
        {
            "level.dat",
            "level.dat_old",
            "region",
            "poi",
            "entities",
            "data",
            "dim-1",
            "dim1",
            "playerdata",
            "stats",
            "advancements",
        }
    ),
    data_root_markers=frozenset({"world", "level.dat", "server.properties"}),
    executable_suffixes=(".jar",),
    bare_world_marker="level.dat",
    # The image regenerates the server jar, loader caches and logs on start.
    backup_exclude_dirs=frozenset({"libraries", ".cache", "versions", "logs", ".fabric"}),
    backup_exclude_files=frozenset({".rcon-cli.env", ".rcon-cli.yaml"}),
    partial_subtrees={},
    placements=(("world", ""),),
    # A singleplayer world folder goes under world/.
    bare_world_placement="world",
)


class _RconAdminClient(AdminClient):
    """Source RCON as the platform's admin interface. save-off/save-on make the
    flush a real freeze."""

    def __init__(self, host: str, port: int, password: str) -> None:
        self._rcon = RconClient(host, port, password)
        self._open = False

    def _connect(self) -> None:
        if not self._open:
            try:
                self._rcon.connect()
            except RconError as exc:
                raise AdminUnavailable(str(exc)) from exc
            self._open = True

    def save_flush(self) -> None:
        self._connect()
        try:
            self._rcon.command("save-off")
            self._rcon.command("save-all flush")
        except RconError as exc:
            raise AdminUnavailable(str(exc)) from exc

    def save_resume(self) -> None:
        """Re-enable autosave after an archive."""
        if not self._open:
            return
        try:
            self._rcon.command("save-on")
        except RconError:
            pass

    def players(self) -> list[PlayerInfo]:
        self._connect()
        try:
            raw = self._rcon.command("list")
        except RconError as exc:
            raise AdminUnavailable(str(exc)) from exc
        # "There are 2 of a max of 20 players online: alice, bob"
        if ":" not in raw:
            return []
        names = [n.strip() for n in raw.split(":", 1)[1].split(",")]
        return [PlayerInfo(name=n) for n in names if n]

    def close(self) -> None:
        if self._open:
            self._rcon.close()
            self._open = False


class MinecraftAdapter(GameAdapter):
    game = "MINECRAFT"
    display_name = "Minecraft"
    # By digest (release 2026.11.0). The image downloads the server jar for
    # VERSION/TYPE itself. Dependabot cannot see a pin in Python;
    # scripts/game-images.sh --check (weekly in CI) reports a newer release.
    image = (
        "itzg/minecraft-server@sha256:"
        "8d5080d8dcd0a2629c2e4f248678373a337a40537ad6ef2857c7dbcda6e3eadd"
    )
    data_mount = "/data"
    # For the entrypoint to chown /data and drop to the run uid.
    container_caps = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "SETGID", "SETUID", "KILL")
    # mc-router routes on the hostname in the handshake: one shared port.
    allocates_port = False
    container_port = 25565
    stop_timeout_seconds = 60
    # Its exit code is honest, so a clean exit can be trusted as one.
    stops_only_via_platform = False
    readiness_timeout_seconds = 300
    # The server giving up. After the first the JVM lives on idle, so only the
    # log shows it (a world converted between loaders listing the old pack).
    fatal_log_patterns = (
        "Failed to load datapacks, can't proceed with server load",
        "Failed to start the minecraft server",
        "Encountered an unexpected exception",
        "Exception in server tick loop",
    )
    # Mostly heap (-Xms/-Xmx), reserved at boot whether or not anyone plays.
    # Sized for vanilla and moderate packs; a heavy pack is a per-world edit.
    default_memory_mb = 4096
    supports_version_pinning = True
    supports_content_catalog = True
    # Access control is whitelist.json, checked against Mojang accounts.
    supports_server_password = False
    # All baked into the container environment.
    spec_fields = ("version", "server_type", "memory_mb", "max_players")
    import_policy = MINECRAFT_IMPORT_POLICY
    settings_model = MinecraftSettings
    enabled_by_default = True
    edge_profile = "minecraft"

    # --- World settings -----------------------------------------------------

    def new_settings(self, raw: dict[str, Any]) -> dict[str, Any]:
        settings = self.validate_settings(raw)
        self._check_server_type(settings["server_type"])
        # Blank pins the latest release; an explicit one is checked against the
        # manifest so a typo fails here, not at boot.
        settings["version"] = settings["version"] or latest_release()
        _check_version_known(settings["version"])
        return settings

    def updated_settings(self, current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        settings = self.validate_settings({**current, **patch})
        if not settings["version"]:
            raise GameSettingsError(f"version: {VERSION_HELP}")
        if "server_type" in patch:
            self._check_server_type(settings["server_type"])
        if "version" in patch:
            _check_version_known(settings["version"])
        return settings

    def settings_risks(self, current: dict[str, Any], new: dict[str, Any]) -> list[str]:
        risks = []
        old_v, new_v = current.get("version") or "", new.get("version") or ""
        old_t, new_t = release_tuple(old_v), release_tuple(new_v)
        if old_t and new_t and new_t < old_t:
            risks.append(
                f"Downgrading the version from {old_v} to {new_v} can "
                f"corrupt or fail to load existing world data."
            )
        if current.get("server_type") and new.get("server_type") != current.get("server_type"):
            risks.append(
                f"Changing the server type from {current['server_type']} to "
                f"{new['server_type']} may break installed mods/plugins and world data."
            )
        return risks

    def check_settings_change(self, db: Session, world: World, new: dict[str, Any]) -> None:
        from .content_api import stranded_by

        stranded = stranded_by(db, world, ServerType(new["server_type"]), new["version"])
        if stranded:
            names = ", ".join(sorted(stranded))
            raise GameSettingsConflict(
                f"Remove {names} before switching to {new['server_type']}: "
                f"{'they are' if len(stranded) > 1 else 'it is'} not compatible with it."
            )

    def settings_schema(self) -> dict[str, Any]:
        schema = super().settings_schema()
        props = schema["properties"]
        # Exactly what create accepts.
        allowed = minecraft_config().server_types
        schema.setdefault("$defs", {}).setdefault("ServerType", {})["enum"] = [
            t.value for t in ServerType if t.value in allowed
        ]
        props["version"]["default"] = latest_release()
        return schema

    def egress_variant(self, settings: dict[str, Any]) -> str:
        # The loader decides which allow-list entries apply.
        return settings.get("server_type") or ""

    def _check_server_type(self, server_type: str) -> None:
        if server_type not in minecraft_config().server_types:
            raise GameSettingsError("Unsupported server type")

    def enable_problems(self) -> list[str]:
        problems = super().enable_problems()
        if not minecraft_config().mc_router_api_url:
            problems.append("PLATFORM_MC_ROUTER_API_URL is not set, so no route can be published.")
        return problems

    # --- What Minecraft adds to the platform --------------------------------

    def config(self) -> MinecraftConfig:
        return minecraft_config()

    def periodic_jobs(self) -> list[PeriodicJob]:
        from .routing import run_sleep_idle, run_sync_routes

        return [
            # mc-router keeps routes in memory and forgets them on restart.
            # Until a world's route exists, players get the placeholder.
            PeriodicJob(id="mc_routes", seconds=20, run=run_sync_routes),
            PeriodicJob(id="mc_idle_sleep", seconds=60, run=run_sleep_idle),
        ]

    def api_routers(self) -> list[APIRouter]:
        from .content_api import admin_router, world_router

        return [admin_router, world_router]

    def job_handlers(self) -> dict[str, Callable[[Session, Job], None]]:
        from .content_jobs import run_content

        return {"CONTENT": lambda db, job: run_content(db, job.world_id, job.payload)}

    def spec_extras(self, db: Session, world: World) -> dict[str, Any]:
        from .content_api import resource_pack_for

        rp = resource_pack_for(db, world)
        if rp is None:
            return {}
        return {
            "resource_pack_url": rp.download_url or "",
            "resource_pack_sha1": rp.sha1 or "",
            "resource_pack_enforce": bool(rp.enforce),
        }

    def managed_content_hashes(self, db: Session, world: World) -> set[str]:
        from .content_jobs import managed_hashes

        return managed_hashes(db, world.id)

    def after_restore(self, db: Session, world: World) -> None:
        from .content_jobs import reinstall_world_content

        reinstall_world_content(db, world)

    def environment(self, spec: WorldSpec) -> dict[str, str]:
        s = get_settings()
        env = {
            "EULA": "TRUE",
            "TYPE": spec.settings.get("server_type") or "VANILLA",
            "VERSION": spec.settings.get("version") or "",
            # A share of the container limit, leaving room off-heap.
            "MEMORY": f"{jvm_heap_mb(spec.memory_mb)}M",
            "MAX_PLAYERS": str(spec.max_players),
            "ENABLE_RCON": "true",
            "RCON_PASSWORD": spec.admin_password,
            "RCON_PORT": str(minecraft_config().rcon_port),
            "ONLINE_MODE": "TRUE",
            # The owner of worlds_dir, shared with the api and worker.
            "UID": str(s.run_uid),
            "GID": str(s.run_gid),
        }
        # Delivered to clients by URL; the image writes server.properties.
        pack_url = spec.extras.get("resource_pack_url")
        if pack_url:
            env["RESOURCE_PACK"] = pack_url
            if spec.extras.get("resource_pack_sha1"):
                env["RESOURCE_PACK_SHA1"] = spec.extras["resource_pack_sha1"]
            env["RESOURCE_PACK_ENFORCE"] = (
                "TRUE" if spec.extras.get("resource_pack_enforce") else "FALSE"
            )
        return env

    # --- Install and play ---------------------------------------------------

    # The world's code. Fabric's install launches the loader, which would load
    # every mod in mods/.
    install_hidden_paths = ("mods", "plugins")

    def _install_key(self, spec: WorldSpec) -> dict[str, str]:
        """What an install is for. A change means installing again."""
        return {
            "image": self.image,
            "type": spec.settings.get("server_type") or "VANILLA",
            "version": spec.settings.get("version") or "",
        }

    def _read_marker(self, data_dir: str) -> dict[str, Any] | None:
        try:
            raw = world_fs.read_bytes(data_dir, INSTALL_MARKER)
            marker = None if raw is None else json.loads(raw)
        except (OSError, ValueError):
            return None
        if not isinstance(marker, dict) or not _SERVER_FILE.fullmatch(str(marker.get("server"))):
            return None
        return marker

    def install_needed(self, spec: WorldSpec, data_dir: str) -> bool:
        marker = self._read_marker(data_dir)
        if marker is None or marker.get("key") != self._install_key(spec):
            return True
        try:
            age = time.time() - float(marker.get("installed_at", 0))
        except (TypeError, ValueError):
            return True
        if not 0 <= age < INSTALL_MAX_AGE_SECONDS:
            return True
        needed = [marker["server"], *(marker.get("outputs") or [])]
        return not all(os.path.exists(os.path.join(data_dir, str(p))) for p in needed)

    def install_environment(self, spec: WorldSpec) -> dict[str, str]:
        env = self.environment(spec)
        server_type = env["TYPE"]
        if server_type == "PAPER":
            # Its jar downloads and patches vanilla on first launch; patch-only
            # does that and exits before loading any plugin.
            env["JVM_DD_OPTS"] = "paperclip.patchonly:true"
        elif server_type == "FABRIC":
            # Its launcher downloads the game and libraries on first launch.
            # Minecraft then prints its usage and exits, loading no world.
            env["EXTRA_ARGS"] = "--help"
        else:
            # The image's setup, with nothing launched.
            env["SETUP_ONLY"] = "true"
        return env

    def _installed_server(self, spec: WorldSpec, data_dir: str) -> str:
        """The server the install left, as a file name in the data dir."""
        key = self._install_key(spec)
        results = _INSTALL_RESULTS.get(key["type"])
        if results is None:
            server = f"minecraft_server.{key['version']}.jar"
        else:
            server = os.path.basename(
                _read_results(os.path.join(data_dir, results)).get("SERVER", "")
            )
        if not _SERVER_FILE.fullmatch(server) or not os.path.isfile(os.path.join(data_dir, server)):
            raise InstallIncomplete(
                f"the {key['type'].lower()} {key['version']} install left no server to run"
            )
        return server

    def finish_install(self, spec: WorldSpec, data_dir: str) -> None:
        marker = {
            "key": self._install_key(spec),
            "server": self._installed_server(spec, data_dir),
            "outputs": [p for p in _INSTALL_OUTPUTS if os.path.exists(os.path.join(data_dir, p))],
            "installed_at": time.time(),
        }
        world_fs.write_text(data_dir, INSTALL_MARKER, json.dumps(marker))

    def run_environment(self, spec: WorldSpec, data_dir: str) -> dict[str, str]:
        marker = self._read_marker(data_dir)
        if marker is None:
            raise InstallIncomplete("no install to run; start the world again to install it")
        env = self.environment(spec)
        env["FAMILY"] = _FAMILY.get(env["TYPE"], "HYBRID")
        env["TYPE"] = "CUSTOM"
        env["CUSTOM_SERVER"] = f"{self.data_mount}/{marker['server']}"
        return env

    def labels(self, spec: WorldSpec) -> dict[str, str]:
        host = f"{spec.subdomain}.{get_settings().servers_domain}"
        return {"mc-router.host": host, "mc-router.port": str(self.container_port)}

    def admin_client(self, host: str, password: str) -> AdminClient:
        return _RconAdminClient(host, minecraft_config().rcon_port, password)

    def connect_address(self, subdomain: str, domain: str, game_port: int | None) -> str:
        return f"{subdomain}.{domain}"


def _check_version_known(version: str) -> None:
    """Refuse a well-formed version Mojang does not ship (``1.21.99``). Only
    when the manifest answers: an outage must not block every create."""
    if version_exists(version) is False:
        raise GameSettingsError(
            f"'{version}' is not a Minecraft version Mojang ships. Releases look "
            f"like 1.21.4 and snapshots like 24w14a; check the spelling."
        )
