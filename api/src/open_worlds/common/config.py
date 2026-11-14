"""Settings, loaded once from the environment and shared by every process.

Secrets have no defaults; each process requires its own at startup.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Renamed settings, mapped to what replaced them. ``extra="ignore"`` would
# otherwise drop an old name silently, so the process refuses to start instead.
_RETIRED_ENV_VARS = {
    "PLATFORM_MC_RUN_UID": "PLATFORM_RUN_UID",
    "PLATFORM_MC_RUN_GID": "PLATFORM_RUN_GID",
    # An env var beside the admin setting would be silently ignored.
    "PLATFORM_ENABLED_GAMES": "Admin > Settings > Games (no longer an environment variable)",
}

_ENV_FILE = ".env"


def _dotenv_keys() -> dict[str, str | None]:
    """Values from the dotenv file, so the retired-name check also sees
    settings never exported into the environment."""
    try:
        from dotenv import dotenv_values

        return dict(dotenv_values(_ENV_FILE))
    except (OSError, ImportError):  # pragma: no cover - no dotenv file or lib
        return {}


def split_keys(raw: str) -> list[str]:
    return [k.strip() for k in raw.split(",") if k.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLATFORM_", env_file=_ENV_FILE, extra="ignore")

    # --- Core / runtime ---
    log_level: str = "INFO"
    log_json: bool = True

    # --- Database ---
    database_url: str = Field(
        default="postgresql+psycopg://games:games@db:5432/games",
        description="SQLAlchemy URL for Postgres (psycopg3 driver).",
    )

    # --- Secrets ---
    # Not validated here: each process calls ``require`` for the secrets it
    # uses, so no container is handed a secret it does not need.
    session_secret: str = Field(default="", description="Secret used to sign session cookies.")
    fernet_key: str = Field(
        default="", description="Fernet key used to encrypt Drive refresh tokens."
    )
    # Rotation: keys that are no longer current but still accepted, comma
    # separated. Cookies signed and tokens encrypted with them keep working;
    # everything new uses the current key. See RUNBOOK "Rotating keys".
    session_old_secrets: str = ""
    fernet_old_keys: str = ""
    session_cookie_name: str = "platform_session"
    session_max_age_seconds: int = 60 * 60 * 24 * 7  # 7 days
    cookie_secure: bool = True

    # --- Google OAuth / OIDC ---
    google_client_id: str = Field(default="", description="Google OAuth client id.")
    google_client_secret: str = Field(default="", description="Google OAuth client secret.")
    oauth_redirect_base: str = Field(
        default="https://games.example.com",
        description="Public base URL used to build OAuth redirect URIs.",
    )
    # Space/comma separated list of Google `sub` values seeded as admins.
    admin_google_subs: str = ""

    # --- Web / CORS ---
    web_origin: str = "https://games.example.com"

    # Local-only sign-in that skips Google; refused unless everything is on
    # loopback (api/dev_login.py). Never passed through by compose.
    dev_login: bool = False

    # --- Domains ---
    # One wildcard domain for every world of every game. mc-router routes on
    # it; for Palworld the port selects the world and the name is cosmetic.
    servers_domain: str = "servers.example.com"

    # Peers whose X-Forwarded-* headers uvicorn trusts: the edge's address in
    # production. Comma-separated IPs, or "*". Defaults to loopback only, so a
    # deploy that loses the setting stops trusting forged headers rather than
    # trusting everyone's.
    forwarded_allow_ips: str = "127.0.0.1"

    # --- Internal service auth ---
    # Required on every /api/internal/* call. internal-api shares the games
    # network with every untrusted world container, so this secret is all that
    # keeps a compromised server from waking and sleeping other worlds.
    internal_secret: str = Field(
        default="", description="Shared secret required on /api/internal/* calls."
    )

    # --- Games ---
    # Compose profiles running here, i.e. which games' routers are up (from
    # COMPOSE_PROFILES). A game can only be enabled while its profile is listed;
    # which games are offered is an admin setting (common/game_availability.py).
    compose_profiles: str = ""
    # Directory of <game>.<ext> card art, which operators supply. See
    # GameAdapter.art_path. Empty: no game has art.
    game_art_dir: str = ""

    # --- Docker orchestration ---
    docker_host: str = "tcp://docker-socket-proxy:2375"
    # Shared by every world container and both routers.
    games_network: str = "games"
    # Its address range. Every world and both routers sit in it, so
    # /metrics refuses peers from it.
    games_subnet: str = "172.19.0.0/16"
    worlds_dir: str = "/srv/games/worlds"
    # So a world's container name can never be a platform container's.
    container_name_prefix: str = "game-world-"
    # uid/gid world containers run as: the owner of worlds_dir, and the uid the
    # api and worker run as.
    run_uid: int = 1000
    run_gid: int = 1000
    # Max processes/threads a world container may spawn (fork-bomb guard).
    container_pids_limit: int = 512
    # Resolver for world containers, as an address: the dns-observer, which
    # records the names worlds look up. Empty keeps Docker's resolver.
    world_dns: str = ""
    # Appended after world_dns, so a dead observer makes lookups slow rather
    # than failing.
    world_dns_fallback: str = "1.1.1.1"
    # The emergency switch: false removes every world's egress guard, for when
    # the guard itself keeps worlds from starting.
    egress_guard_enabled: bool = True
    # Whether a new world starts with egress blocking on; off records only.
    # Each world keeps its own value after creation.
    egress_blocking_default: bool = False
    # Built locally: the policy is baked in, so a rebuild is how a policy change
    # ships, and guards built from an older image are replaced.
    egress_guard_image: str = "open-worlds-egress-guard:latest"
    # How long to wait for a guard to prove its firewall before giving up.
    egress_guard_ready_seconds: float = 20.0
    # Port on the dns-observer (world_dns) that guards report refusals to.
    dns_observer_report_port: int = 5354
    # Who may change a world's blocking: "none" (setting hidden), "admin", or
    # "owner_and_manager" (and admins).
    egress_override_actors: Literal["none", "admin", "owner_and_manager"] = "none"
    # CPU cores per world container (fractional allowed); 0 is uncapped. No
    # safe default: it depends on the host's cores and how many worlds run.
    container_cpus: float = 0.0
    label_namespace: str = "open-worlds"

    # --- Game ports (games with allocates_port, i.e. Palworld) ---
    # One UDP port per world for games whose protocol carries no hostname.
    # pal-waker publishes this range, so it must match its compose service.
    game_port_range_start: int = 8211
    game_port_range_end: int = 8220
    admin_password_bytes: int = 24

    # --- World defaults / caps ---
    # Unset: each game uses its own default (GameAdapter.default_memory_mb),
    # since it is reserved heap for one and a ceiling for another. Set, it
    # overrides every game's. The bounds below are global either way.
    default_memory_mb: int | None = None
    max_memory_mb: int = 32768
    # Worlds too starved to boot are refused. 1024 is the least either game
    # runs in.
    min_memory_mb: int = 1024
    # Total MiB of world memory limits the host allows at once, running and
    # sleeping (a connection wakes a sleeping world unasked). A start past it is
    # refused instead of letting the kernel pick a victim. Unset: no budget.
    host_memory_budget_mb: int | None = None
    max_worlds_per_user: int = 10
    default_max_players: int = 16
    max_players_cap: int = 64

    # Idle time before a world is put to sleep (seconds): by the worker for
    # Minecraft, by pal-waker for Palworld.
    auto_stop_after_seconds: int = 600
    # After a start, how long before the idle timer may fire, so a slow-booting
    # world is not put straight back to sleep.
    wake_grace_seconds: int = 180

    # How often running worlds' player counts are read. Also bounds idle-sleep,
    # which ignores a count older than three ticks.
    player_poll_seconds: int = 10

    # How often booting worlds are probed for readiness. Fast and cheap: only
    # worlds currently booting are probed.
    readiness_probe_interval_seconds: int = 3
    # How often booting worlds' logs are checked for a failed start
    # (worker/failures.py).
    startup_failure_check_seconds: int = 15
    # Running but silent this long counts as unresponsive. Fallback only: the
    # game's own GameAdapter.readiness_timeout_seconds normally applies.
    readiness_timeout_seconds: int = 300

    # Fallback graceful stop window when a container's game is unknown.
    stop_timeout_seconds: int = 60

    # --- Worker maintenance / resilience ---
    maintenance_interval_seconds: int = 120
    # Jobs on one world never overlap, so this bounds how many worlds are
    # worked on at once (they share a disk).
    job_concurrency: int = 5
    # A job RUNNING longer than this was orphaned by a crashed worker: requeued,
    # or failed once attempts run out.
    job_stuck_after_seconds: int = 1800
    job_max_attempts: int = 3
    # Then its teardown is retried.
    delete_stuck_after_seconds: int = 300
    # A world STOPPING this long was abandoned mid-stop; the worker finishes it.
    # Above the longest graceful window (Palworld's 300s).
    stop_stuck_after_seconds: int = 600
    # An uncommitted staged upload this old is removed, so it cannot block
    # future uploads.
    staged_upload_ttl_seconds: int = 3600

    # --- Backups ---
    backup_staging_dir: str = "/srv/games/backup-staging"
    # Assumed ratio of an extracted import to its archive, for the disk check.
    import_expansion_factor: int = 4
    # Disk that must stay free after staging a backup; worlds share the disk.
    min_free_bytes_for_backup: int = 5 * 1024 * 1024 * 1024  # 5 GiB
    # Caps on restoring a Drive backup, download and uncompressed. Above the
    # upload caps because a world can outgrow what may be uploaded, but a
    # Drive file can be replaced by its owner, so not unlimited.
    max_restore_bytes: int = 20 * 1024 * 1024 * 1024  # 20 GiB
    max_restore_file_count: int = 500_000
    # Timeout for calls to a world's admin interface (save flush, player list).
    admin_api_timeout_seconds: float = 10.0

    # Uploads above this spill to the staging volume rather than memory.
    upload_spool_max_bytes: int = 8 * 1024 * 1024

    # --- World manager ---
    # The process that holds the Docker credential. Set on api and worker, not
    # on the world manager itself. Empty: drive Docker directly (tests, dev).
    world_manager_url: str = ""
    world_manager_secret: str = ""

    # --- Admin settings (common/runtime_config.py) ---
    # A JSON file shared by api and worker, edited from the UI.
    runtime_config_path: str = "/srv/games/platform-config/settings.json"
    runtime_config_ttl_seconds: int = 30

    @field_validator("admin_google_subs", "compose_profiles")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("default_memory_mb", "host_memory_budget_mb", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        """Empty means unset: compose's ``${VAR:-}`` defines the variable even
        when .env does not, and "" would not parse as an int."""
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @model_validator(mode="after")
    def _reject_retired_env_vars(self) -> Settings:
        """Refuse to start while a retired setting name is still set, in the
        environment or in a local ``.env``."""
        stale = {name for name in _RETIRED_ENV_VARS if os.environ.get(name)}
        stale |= {name for name in _RETIRED_ENV_VARS if _dotenv_keys().get(name)}
        if stale:
            renames = ", ".join(f"{name} -> {_RETIRED_ENV_VARS[name]}" for name in sorted(stale))
            raise ValueError(
                f"Retired environment variable(s) still set: {renames}. These are no "
                f"longer read; move them as shown, or the setting they configure "
                f"silently falls back to its default."
            )
        return self

    @model_validator(mode="after")
    def _check_memory_bounds(self) -> Settings:
        """A default outside its own bounds would fail every create that took it."""
        if self.min_memory_mb > self.max_memory_mb:
            raise ValueError(
                f"PLATFORM_MIN_MEMORY_MB ({self.min_memory_mb}) must not exceed "
                f"PLATFORM_MAX_MEMORY_MB ({self.max_memory_mb})."
            )
        # An unset default lets each game decide, clamped at creation.
        if self.default_memory_mb is not None and not (
            self.min_memory_mb <= self.default_memory_mb <= self.max_memory_mb
        ):
            raise ValueError(
                f"PLATFORM_DEFAULT_MEMORY_MB ({self.default_memory_mb}) must fall "
                f"between PLATFORM_MIN_MEMORY_MB ({self.min_memory_mb}) and "
                f"PLATFORM_MAX_MEMORY_MB ({self.max_memory_mb})."
            )
        return self

    @model_validator(mode="after")
    def _check_port_range(self) -> Settings:
        if self.game_port_range_start < 1024 or self.game_port_range_end > 65535:
            raise ValueError("Game port range must fall within 1024-65535.")
        if self.game_port_range_end < self.game_port_range_start:
            raise ValueError("Game port range end must not be below its start.")
        return self

    def require(self, *names: str) -> None:
        """Fail fast if any of the named secrets is unset. Each process calls
        this with exactly the secrets it uses."""
        missing = [n for n in names if not str(getattr(self, n)).strip()]
        if missing:
            env = ", ".join(f"PLATFORM_{n.upper()}" for n in missing)
            raise RuntimeError(f"Required setting(s) not set: {env}")

    @property
    def profiles(self) -> set[str]:
        """Compose profiles active on this host, lower case."""
        return {p.strip().lower() for p in self.compose_profiles.split(",") if p.strip()}

    @property
    def admin_subs(self) -> set[str]:
        raw = self.admin_google_subs.replace(",", " ").split()
        return {s.strip() for s in raw if s.strip()}

    @property
    def game_ports(self) -> range:
        """Every host port the platform may allocate to a world."""
        return range(self.game_port_range_start, self.game_port_range_end + 1)

    @property
    def max_worlds(self) -> int:
        """Hard platform-wide cap: one world per allocatable port."""
        return len(self.game_ports)

    @property
    def google_redirect_uri(self) -> str:
        return f"{self.oauth_redirect_base.rstrip('/')}/api/auth/google/callback"

    @property
    def google_drive_redirect_uri(self) -> str:
        return f"{self.oauth_redirect_base.rstrip('/')}/api/auth/drive/callback"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


class GameConfig(BaseSettings):
    """A game's own ``PLATFORM_*`` settings, read exactly like :class:`Settings`."""

    model_config = SettingsConfigDict(env_prefix="PLATFORM_", env_file=_ENV_FILE, extra="ignore")


_game_configs: dict[type, tuple[Settings, GameConfig]] = {}


def game_config[T: GameConfig](cls: type[T]) -> T:
    """A game's settings, cached as long as :func:`get_settings` (clearing that
    cache, as tests do, re-reads these too)."""
    s = get_settings()
    hit = _game_configs.get(cls)
    if hit is None or hit[0] is not s:
        hit = (s, cls())
        _game_configs[cls] = hit
    return hit[1]  # type: ignore[return-value]
