"""Settings only Minecraft reads. Same ``PLATFORM_*`` names as ever."""

from __future__ import annotations

from pydantic import field_validator

from ...config import GameConfig, game_config


class MinecraftConfig(GameConfig):
    # RCON, reachable only on the private Docker network.
    rcon_port: int = 25575

    # --- Version pinning ---
    # Worlds are pinned to a concrete release; the default offered at create-time
    # is the latest stable release Mojang ships. The fallback covers manifest
    # downtime.
    mojang_version_manifest_url: str = (
        "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"
    )
    version_cache_ttl_seconds: int = 6 * 60 * 60  # 6 hours
    default_world_version_fallback: str = "1.21.4"
    allowed_server_types: str = "VANILLA,PAPER,FABRIC,FORGE"

    # --- Curated content catalog ---
    modrinth_api_base: str = "https://api.modrinth.com/v2"
    modrinth_user_agent: str = "open-worlds/1.0 (self-hosted)"
    content_http_timeout_seconds: int = 30
    max_mod_file_bytes: int = 256 * 1024 * 1024
    max_datapack_file_bytes: int = 128 * 1024 * 1024
    max_resourcepack_file_bytes: int = 250 * 1024 * 1024
    content_cache_dir: str = ""

    # --- mc-router ---
    # Its route-management API. The worker publishes a route for every RUNNING
    # Minecraft world and withdraws the rest, so mc-router needs no Docker
    # access to discover them. Empty disables publishing.
    mc_router_api_url: str = ""
    mc_router_timeout_seconds: float = 5.0

    @field_validator("allowed_server_types")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @property
    def server_types(self) -> set[str]:
        return {s.strip().upper() for s in self.allowed_server_types.split(",") if s.strip()}


def minecraft_config() -> MinecraftConfig:
    return game_config(MinecraftConfig)
