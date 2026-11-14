import type { DriveBackup, RestorableBackup } from "./types";

/**
 * One <select> offering two kinds of thing: a tracked backup (restored through
 * `backup_id`) or an untracked Drive archive (through `drive_file_id`). The API
 * takes exactly one, so the option value carries which it is.
 */
type BackupChoice =
  { kind: "backup"; id: string } | { kind: "drive"; id: string } | null;

/** "" for no choice is deliberate: it is what an empty <option> carries. */
function encodeChoice(choice: BackupChoice): string {
  if (!choice) return "";
  return `${choice.kind}:${choice.id}`;
}

export function decodeChoice(value: string): BackupChoice {
  if (!value) return null;
  const at = value.indexOf(":");
  if (at < 0) return null;
  const kind = value.slice(0, at);
  const id = value.slice(at + 1);
  if (!id) return null;
  if (kind === "backup") return { kind: "backup", id };
  if (kind === "drive") return { kind: "drive", id };
  return null;
}

/** Whichever of the two ids is set, as a single value for a <select>. */
export function currentChoice(backupId: string, driveFileId: string): string {
  if (backupId) return encodeChoice({ kind: "backup", id: backupId });
  if (driveFileId) return encodeChoice({ kind: "drive", id: driveFileId });
  return "";
}

export function backupLabel(b: RestorableBackup): string {
  return `${b.world_display_name} · ${b.filename}`;
}

function sizeLabel(bytes: number | null): string {
  if (bytes === null) return "";
  const mb = bytes / (1024 * 1024);
  return mb >= 1024
    ? `${(mb / 1024).toFixed(1)} GB`
    : `${Math.max(1, Math.round(mb))} MB`;
}

function dateLabel(iso: string | null): string {
  if (!iso) return "";
  return new Date(iso).toLocaleDateString();
}

export function driveLabel(f: DriveBackup): string {
  const parts = [`${f.world_subdomain} · ${f.filename}`];
  if (f.size_bytes !== null) parts.push(sizeLabel(f.size_bytes));
  if (f.modified_at) parts.push(dateLabel(f.modified_at));
  return parts.join(" · ");
}
