import type { GameInfo, Limits, Me, World } from "../types";

/** A world with every field filled, overridden per test. */
export function makeWorld(over: Partial<World> = {}): World {
  return {
    id: "w1",
    game: "MINECRAFT",
    subdomain: "alpha",
    display_name: "Alpha",
    memory_mb: 4096,
    max_players: 10,
    state: "OFF",
    game_port: null,
    settings: {},
    reported_version: null,
    server_password: "",
    player_count: 0,
    players_updated_at: null,
    readiness: "NOT_RUNNING",
    auto_backup_enabled: false,
    auto_backup_interval: "DAILY",
    auto_backup_max: 5,
    egress_guarded: false,
    egress_log_only: false,
    egress_open_exception: false,
    last_error: null,
    last_error_summary: null,
    last_error_at: null,
    created_at: "2026-01-01T00:00:00Z",
    last_active_at: null,
    started_at: null,
    connect_address: "alpha.servers.example.com",
    owner_email: "owner@example.com",
    owner_id: "u-owner",
    access_level: "OWNER",
    icon_version: null,
    icon_is_custom: false,
    ...over,
  };
}

export function makeMe(over: Partial<Me> = {}): Me {
  return {
    id: "u-owner",
    google_sub: "sub",
    email: "owner@example.com",
    display_name: "Owner",
    role: "USER",
    drive_linked: true,
    is_admin: false,
    ...over,
  };
}

export function makeGame(over: Partial<GameInfo> = {}): GameInfo {
  return {
    game: "MINECRAFT",
    display_name: "Minecraft",
    supports_version_pinning: true,
    supports_content_catalog: true,
    supports_server_password: false,
    allocates_port: false,
    settings_schema: { type: "object", properties: {} },
    spec_fields: [],
    art_url: null,
    default_memory_mb: 4096,
    ports_available: null,
    ...over,
  };
}

export function makeLimits(over: Partial<Limits> = {}): Limits {
  return {
    max_worlds_per_user: 3,
    max_upload_bytes: 1024 ** 3,
    max_memory_mb: 16384,
    min_memory_mb: 1024,
    default_max_players: 10,
    max_players_cap: 64,
    servers_domain: "servers.example.com",
    egress_override_actors: "none",
    egress_blocking_default: true,
    games: [makeGame()],
    ...over,
  };
}
