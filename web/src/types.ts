type Role = "USER" | "ADMIN";

/** Per-world access. Distinct from Role, which is platform-wide.
 *  NONE never reaches the client: a world you cannot see is a 404. */
export type AccessLevel = "OPERATOR" | "MANAGER" | "OWNER";

/** The tiers that can actually be granted. OWNER is not grantable — it is
 *  the world's owner_id, not a membership. */
export type CollaboratorLevel = "OPERATOR" | "MANAGER";

export interface Collaborator {
  user_id: string;
  email: string | null;
  display_name: string | null;
  level: CollaboratorLevel;
  created_at: string;
}

export type WorldState =
  | "RUNNING"
  | "SLEEPING"
  /** A stop was requested; the server is shutting down gracefully (minutes,
   *  for some games). Resolves to OFF or SLEEPING. Controls are locked meanwhile. */
  | "STOPPING"
  | "OFF"
  | "ERROR"
  | "DELETING";

/** Which game a world runs: an adapter id from the API (`MINECRAFT`). A string
 *  rather than a union, because the API decides which games exist; what each
 *  one supports comes from `/api/games` (see `src/games/`). */
type GameId = string;

/**
 * How far a running world is toward accepting players.
 *
 * `UNRESPONSIVE` is a real fault, not a slow boot: the container has been up
 * well past the boot window and its admin interface has never answered.
 */
export type Readiness = "NOT_RUNNING" | "STARTING" | "READY" | "UNRESPONSIVE";

export type AutoBackupInterval = "DAILY" | "WEEKLY" | "MONTHLY";
type BackupKind = "AUTO" | "MANUAL";
type BackupStatus = "PENDING" | "RUNNING" | "SUCCESS" | "FAILED";

export interface Me {
  id: string;
  google_sub: string;
  email: string | null;
  display_name: string | null;
  role: Role;
  drive_linked: boolean;
  is_admin: boolean;
}

/** One icon file a world has; what clearing the icon deletes. */
export interface IconFile {
  /** "server": the world's own icon. "save": one the game keeps in the save
   *  folder and falls back to. */
  kind: "server" | "save";
  /** Relative to the world's data directory. */
  path: string;
  /** Whether the game would show it (a 64x64 PNG). */
  valid: boolean;
  version: number;
}

export interface World {
  id: string;
  game: GameId;
  subdomain: string;
  display_name: string;
  memory_mb: number;
  max_players: number;
  state: WorldState;
  /** The world's own port, for games that allocate one. Null otherwise. */
  game_port: number | null;
  /** The game's own settings (its schema is on GameInfo). */
  settings: GameSettings;
  /** What the running server last reported, for games that cannot pin one. */
  reported_version: string | null;
  /** The join password in clear, for games that have one. Empty means the world
   *  is open to anyone who knows its address. Returned only to the owner and
   *  admins — unlike the admin password, this one exists to be shared. */
  server_password: string;
  /** Live player count from the worker's polling. Advisory: stale when the
   *  world isn't running or the last poll failed. */
  player_count: number;
  players_updated_at: string | null;
  /** Whether a RUNNING world is actually joinable. RUNNING only means the
   *  container is up; the server inside takes 30-60s+ to accept connections. */
  readiness: Readiness;
  auto_backup_enabled: boolean;
  auto_backup_interval: AutoBackupInterval;
  auto_backup_max: number;
  /** Whether this world runs behind an egress guard (outbound allow-list). */
  egress_guarded: boolean;
  /** "Allow unknown network connections": the guard records but blocks
   * nothing. Set at creation from the platform default. */
  egress_log_only: boolean;
  /** Blocking is off on a platform that blocks new worlds by default -- the
   * case worth flagging, unlike every world on a platform that does not. */
  egress_open_exception: boolean;
  /** Why the server failed on its current or last boot: an excerpt of its
   * log (redacted), the line that matters, and when it was recorded. Null
   * when nothing failed; cleared each time the world starts again. */
  last_error: string | null;
  last_error_summary: string | null;
  last_error_at: string | null;
  created_at: string;
  /** Last player poll that found anyone online; null if never seen occupied. */
  last_active_at: string | null;
  /** Last time the server started, a player's wake-up included. */
  started_at: string | null;
  connect_address: string;
  /** Always present. */
  owner_email: string | null;
  owner_id: string;
  /** What the signed-in user may do to this world, decided by the server.
   *  Required, so an `undefined` cannot slip through a truthiness check. */
  access_level: AccessLevel;
  /** When the world's icon last changed (ms), or null when it has none. Also
   *  the icon URL's cache-buster, so a replaced icon shows at once. */
  icon_version: number | null;
  /** Whether that icon is the world's own (removable) rather than the save
   *  folder's, which the game keeps. */
  icon_is_custom: boolean;
}

export interface Backup {
  id: string;
  kind: BackupKind;
  status: BackupStatus;
  filename: string;
  size_bytes: number | null;
  created_at: string;
  completed_at: string | null;
  error: string | null;
}

export interface RestorableBackup {
  id: string;
  world_id: string;
  /** The game this backup came from, so a restore cannot cross games. */
  game: GameId;
  world_display_name: string;
  world_subdomain: string;
  kind: BackupKind;
  filename: string;
  size_bytes: number | null;
  created_at: string;
}

/** An archive found in the owner's Drive, tracked by us or not.
 *
 * `known: false` means the backup row is gone -- its world was deleted -- so
 * this file is invisible to the normal restore list even though it is still
 * sitting in Drive. `world_subdomain` comes from the Drive folder it lives in,
 * which is all that survives of the world it was taken from.
 */
export interface DriveBackup {
  drive_file_id: string;
  filename: string;
  world_subdomain: string;
  /**
   * Recovered from the folder the archive sits in. Null for archives written
   * before the Drive layout carried a game level, which cannot be classified
   * after the fact -- their world is gone and nothing else recorded it.
   */
  game: GameId | null;
  size_bytes: number | null;
  modified_at: string | null;
  known: boolean;
}

export interface PlatformUser {
  id: string;
  google_sub: string;
  email: string | null;
  display_name: string | null;
  role: Role;
  drive_linked: boolean;
  created_at: string;
}

/** One installed game, as Admin > Settings > Games shows it. */
export interface AdminGame {
  game: string;
  display_name: string;
  enabled: boolean;
  world_count: number;
  /** What this host lacks for the game to be offered. Empty: nothing. */
  problems: string[];
}

export interface RuntimeSettings {
  max_upload_bytes: number;
  max_upload_file_count: number;
  extra_allowed_root_names: string[];
}

/** A world's game settings, validated by its game's adapter on the API. */
export type GameSettings = Record<string, unknown>;

/** JSON schema, as pydantic writes it. Only what the settings form reads. */
export interface JsonSchema {
  type?: string;
  title?: string;
  description?: string;
  default?: unknown;
  enum?: unknown[];
  maxLength?: number;
  minimum?: number;
  maximum?: number;
  $ref?: string;
  allOf?: JsonSchema[];
  properties?: Record<string, JsonSchema>;
  $defs?: Record<string, JsonSchema>;
}

/** What one game supports, so forms are built from it rather than from
 *  hard-coded knowledge of which game has which fields. */
export interface GameInfo {
  game: GameId;
  display_name: string;
  supports_version_pinning: boolean;
  supports_content_catalog: boolean;
  /** Whether the game's server takes a shared join password. */
  supports_server_password: boolean;
  allocates_port: boolean;
  /** The game's own settings, with this host's choices and defaults filled in. */
  settings_schema: JsonSchema;
  /** Fields (settings keys included) that can only change while a world is off. */
  spec_fields: string[];
  /** Card artwork, when the operator supplied some (PLATFORM_GAME_ART_DIR). */
  art_url: string | null;
  /** Memory a new world of this game starts at, already clamped to the host's
   *  bounds. Per-game because the number isn't comparable across games: for
   *  some it is reserved at boot, for others only a ceiling. */
  default_memory_mb: number;
  /** Ports left for this game; null when it doesn't allocate any. */
  ports_available: number | null;
}

export interface Limits {
  max_worlds_per_user: number;
  max_upload_bytes: number;
  /** Host-wide bounds. There's no global default beside them: what a new world
   *  starts at depends on its game, and lives on GameInfo. */
  max_memory_mb: number;
  min_memory_mb: number;
  default_max_players: number;
  max_players_cap: number;
  /** Wildcard domain worlds are addressed under, e.g. "servers.example.com". */
  servers_domain: string;
  /** Who may set "Allow unknown network connections": nobody, admins, or
   * anyone who manages the world (owner, managers, admins). Mirrors
   * PLATFORM_EGRESS_OVERRIDE_ACTORS. */
  egress_override_actors: "none" | "admin" | "owner_and_manager";
  /** Whether new worlds start with egress blocking on. */
  egress_blocking_default: boolean;
  games: GameInfo[];
}

/** What an import will do with a save folder's icon so the world keeps one,
 *  in the game's own path. */
export interface IconChange {
  /** "delete": server-icon.png is also there, so the save's is never shown.
   *  "move": the save's is the only one, so it becomes server-icon.png. */
  action: "delete" | "move";
  path: string;
  to: string | null;
}

export interface StagedUpload {
  id: string;
  filename: string;
  size_bytes: number;
  file_count: number;
  unknown_entries: string[];
  icon_changes: IconChange[];
}

export type ContentKind = "MOD" | "DATAPACK" | "RESOURCEPACK";
export type ContentSource = "MODRINTH" | "PINNED";

export interface CatalogEntry {
  id: string;
  kind: ContentKind;
  source: ContentSource;
  project_slug: string;
  name: string;
  icon_url: string | null;
  enabled: boolean;
  loaders: string[];
  game_versions: string[];
  created_at: string;
}

export interface AvailableContent {
  catalog_id: string;
  kind: ContentKind;
  source: ContentSource;
  name: string;
  icon_url: string | null;
  installed: boolean;
  server_side_only: boolean | null;
  /** Resource packs only, and only when installed: whether clients must accept
   *  the pack. Null otherwise. */
  enforce: boolean | null;
}

export interface WorldContent {
  id: string;
  catalog_id: string;
  kind: ContentKind;
  name: string;
  icon_url: string | null;
  filename: string | null;
  version_id: string | null;
  installed_at: string;
}

/** Queued work the caller can follow. Content changes answer 202, so the job id
 *  is the only way to tell "still installing" from "failed". */
export interface JobAccepted {
  message: string;
  job_id: string;
}

type JobState = "PENDING" | "RUNNING" | "SUCCESS" | "FAILED";

export interface JobStatus {
  id: string;
  type: string;
  status: JobState;
  /** Set only on FAILED. */
  error: string | null;
  attempts: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}
