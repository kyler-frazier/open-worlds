import type {
  AdminGame,
  Collaborator,
  CollaboratorLevel,
  AvailableContent,
  Backup,
  CatalogEntry,
  ContentKind,
  DriveBackup,
  GameInfo,
  GameSettings,
  IconFile,
  JobAccepted,
  JobStatus,
  Limits,
  Me,
  PlatformUser,
  RestorableBackup,
  RuntimeSettings,
  StagedUpload,
  World,
  WorldContent,
} from "./types";

const BASE = "/api";

export class ApiError extends Error {
  status: number;
  /** The raw `detail` from the API. Usually a string (same as `message`), but
   *  some refusals carry structure -- e.g. revoking a user who still owns
   *  worlds returns `{ message, worlds }` so the UI can show what's in the way. */
  detail: unknown;
  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.status = status;
    this.detail = detail ?? message;
  }
}

async function errorFromResponse(res: Response): Promise<ApiError> {
  let message = res.statusText;
  let detail: unknown = undefined;
  try {
    const body = await res.json();
    detail = body.detail;
    if (typeof detail === "string") message = detail;
    else if (detail && typeof detail === "object" && "message" in detail)
      message = String((detail as { message: unknown }).message);
  } catch {
    /* ignore non-JSON error bodies */
  }
  return new ApiError(res.status, message, detail);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    credentials: "include",
    // X-Requested-With is our CSRF defense in depth; the API requires it on
    // state-changing requests (a cross-site request cannot set custom headers).
    headers: {
      "Content-Type": "application/json",
      "X-Requested-With": "XMLHttpRequest",
      ...(init?.headers ?? {}),
    },
    ...init,
  });
  if (!res.ok) throw await errorFromResponse(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

async function upload<T>(
  path: string,
  formData: FormData,
  method: "POST" | "PUT" = "POST",
): Promise<T> {
  // No Content-Type header: the browser sets the multipart boundary.
  const res = await fetch(`${BASE}${path}`, {
    method,
    credentials: "include",
    headers: { "X-Requested-With": "XMLHttpRequest" },
    body: formData,
  });
  if (!res.ok) throw await errorFromResponse(res);
  return (await res.json()) as T;
}

export const api = {
  // Auth
  me: () => request<Me>("/auth/me"),
  loginUrl: () => `${BASE}/auth/google/login`,
  driveLinkUrl: () => `${BASE}/auth/drive/link`,
  logout: () =>
    request<{ message: string }>("/auth/logout", { method: "POST" }),
  // Ends every session this user holds, this browser included.
  logoutEverywhere: () =>
    request<{ message: string }>("/auth/logout-all", { method: "POST" }),

  // Worlds
  listWorlds: () => request<World[]>("/worlds"),
  getLimits: () => request<Limits>("/worlds/limits"),
  listGames: () => request<GameInfo[]>("/games"),
  createWorld: (body: {
    game: string;
    display_name: string;
    subdomain: string;
    memory_mb?: number;
    max_players?: number;
    // Games with supports_server_password only; rejected by the API otherwise.
    server_password?: string;
    // The game's own settings; the API validates them against its schema.
    settings?: GameSettings;
  }) =>
    request<World>("/worlds", { method: "POST", body: JSON.stringify(body) }),
  startWorld: (id: string) =>
    request<World>(`/worlds/${id}/start`, { method: "POST" }),
  stopWorld: (id: string) =>
    request<World>(`/worlds/${id}/stop`, { method: "POST" }),
  deleteWorld: (id: string) =>
    request<{ message: string }>(`/worlds/${id}`, { method: "DELETE" }),
  // The icon is a plain <img src>: the session cookie rides along, and the
  // version in the query makes a replaced icon a new URL.
  worldIconUrl: (id: string, version: number) =>
    `${BASE}/worlds/${id}/icon?v=${version}`,
  uploadWorldIcon: (id: string, png: Blob) => {
    const fd = new FormData();
    fd.append("file", png, "icon.png");
    return upload<World>(`/worlds/${id}/icon`, fd, "PUT");
  },
  worldIconFileUrl: (id: string, kind: IconFile["kind"], version: number) =>
    `${BASE}/worlds/${id}/icon?kind=${kind}&v=${version}`,
  listWorldIconFiles: (id: string) =>
    request<IconFile[]>(`/worlds/${id}/icon/files`),
  // Clears every icon file, the save's included -- see listWorldIconFiles.
  clearWorldIcon: (id: string) =>
    request<World>(`/worlds/${id}/icon`, { method: "DELETE" }),
  updateWorld: (id: string, body: Record<string, unknown>) =>
    request<World>(`/worlds/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  listBackups: (id: string) => request<Backup[]>(`/worlds/${id}/backups`),
  createBackup: (id: string) =>
    request<Backup>(`/worlds/${id}/backups`, {
      method: "POST",
      body: JSON.stringify({ kind: "MANUAL" }),
    }),
  deleteBackup: (worldId: string, backupId: string) =>
    request<{ message: string }>(`/worlds/${worldId}/backups/${backupId}`, {
      method: "DELETE",
    }),

  // Collaborators
  listCollaborators: (worldId: string) =>
    request<Collaborator[]>(`/worlds/${worldId}/collaborators`),
  addCollaborator: (worldId: string, email: string, level: CollaboratorLevel) =>
    request<Collaborator>(`/worlds/${worldId}/collaborators`, {
      method: "POST",
      body: JSON.stringify({ email, level }),
    }),
  updateCollaborator: (
    worldId: string,
    userId: string,
    level: CollaboratorLevel,
  ) =>
    request<Collaborator>(`/worlds/${worldId}/collaborators/${userId}`, {
      method: "PATCH",
      body: JSON.stringify({ level }),
    }),
  removeCollaborator: (worldId: string, userId: string) =>
    request<{ message: string }>(`/worlds/${worldId}/collaborators/${userId}`, {
      method: "DELETE",
    }),

  // Import
  listRestorableBackups: () =>
    request<RestorableBackup[]>("/worlds/restorable-backups"),
  preflightImport: (id: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return upload<StagedUpload>(`/worlds/${id}/import/preflight`, form);
  },
  preflightImportFromDrive: (id: string, backup_id: string) =>
    request<StagedUpload>(`/worlds/${id}/import/preflight-from-drive`, {
      method: "POST",
      body: JSON.stringify({ backup_id }),
    }),
  listDriveBackups: () => request<DriveBackup[]>("/worlds/drive-backups"),
  // Restore straight from a Drive file, for an archive we no longer have a row
  // for. The server proves ownership through the caller's own Drive token.
  preflightImportFromDriveFile: (id: string, drive_file_id: string) =>
    request<StagedUpload>(`/worlds/${id}/import/preflight-from-drive`, {
      method: "POST",
      body: JSON.stringify({ drive_file_id }),
    }),
  getStagedImport: (id: string) =>
    request<StagedUpload>(`/worlds/${id}/import/staged`),
  deleteStagedImport: (id: string) =>
    request<{ message: string }>(`/worlds/${id}/import/staged`, {
      method: "DELETE",
    }),
  commitImport: (id: string, body: { prune: boolean }) =>
    request<{ message: string }>(`/worlds/${id}/import/commit`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // Admin
  adminListUsers: () => request<PlatformUser[]>("/admin/users"),
  adminAddUser: (google_sub: string, role: string = "USER") =>
    request<PlatformUser>("/admin/users", {
      method: "POST",
      body: JSON.stringify({ google_sub, role }),
    }),
  // Ends every session the user holds; the account is untouched.
  adminForceLogout: (id: string) =>
    request<{ message: string }>(`/admin/users/${id}/logout`, {
      method: "POST",
    }),
  // Without `deleteWorlds` the API refuses (409) when the user still owns
  // worlds and lists them in `detail.worlds`; with it, those worlds are torn
  // down first, then the user.
  adminRevokeUser: (id: string, opts: { deleteWorlds?: boolean } = {}) =>
    request<{ message: string }>(
      `/admin/users/${id}${opts.deleteWorlds ? "?delete_worlds=true" : ""}`,
      { method: "DELETE" },
    ),
  adminListWorlds: () => request<World[]>("/admin/worlds"),
  adminGetSettings: () => request<RuntimeSettings>("/admin/settings"),
  adminListGames: () => request<AdminGame[]>("/admin/games"),
  adminSetGames: (enabled: string[]) =>
    request<AdminGame[]>("/admin/games", {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    }),
  adminUpdateSettings: (body: Partial<RuntimeSettings>) =>
    request<RuntimeSettings>("/admin/settings", {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  // Admin content catalog
  adminListCatalog: (kind?: ContentKind) =>
    request<CatalogEntry[]>(`/admin/content${kind ? `?kind=${kind}` : ""}`),
  adminAddCatalog: (body: {
    kind: ContentKind;
    source: "MODRINTH" | "PINNED";
    project_slug: string;
    name?: string;
    loaders?: string[];
    game_versions?: string[];
    download_url?: string;
    sha512?: string;
    sha1?: string;
  }) =>
    request<CatalogEntry>("/admin/content", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  adminUpdateCatalog: (id: string, body: { enabled?: boolean }) =>
    request<CatalogEntry>(`/admin/content/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  adminDeleteCatalog: (id: string) =>
    request<{ message: string }>(`/admin/content/${id}`, { method: "DELETE" }),

  // Per-world content
  listWorldContent: (id: string) =>
    request<WorldContent[]>(`/worlds/${id}/content`),
  availableContent: (id: string, kind: ContentKind) =>
    request<AvailableContent[]>(`/worlds/${id}/content/available?kind=${kind}`),
  applyContent: (id: string, body: { catalog_id: string; enforce?: boolean }) =>
    request<JobAccepted>(`/worlds/${id}/content`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  removeContent: (id: string, catalogId: string) =>
    request<JobAccepted>(`/worlds/${id}/content/${catalogId}`, {
      method: "DELETE",
    }),
  jobStatus: (id: string, jobId: string) =>
    request<JobStatus>(`/worlds/${id}/jobs/${jobId}`),
};
