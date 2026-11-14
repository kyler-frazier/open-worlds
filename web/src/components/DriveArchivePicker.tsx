import { useState } from "react";
import { backupLabel, currentChoice, driveLabel } from "../backupChoice";
import type { DriveBackup, RestorableBackup } from "../types";

type Props = {
  /**
   * Tracked backups that belong here rather than in the list above -- in
   * practice the caller's other worlds of the same game. Already filtered by
   * the caller, which owns the question of what "above" contains.
   */
  otherBackups: RestorableBackup[];
  /** Untracked archives that do not belong in the list above either. */
  otherFiles: DriveBackup[];
  /** Null while the caller is still loading. */
  loading: boolean;
  /** Selected backup id, or "". Mutually exclusive with driveFileId. */
  backupId: string;
  /** Selected Drive file id, or "". Mutually exclusive with backupId. */
  driveFileId: string;
  onPickBackup: (backupId: string) => void;
  onPickDriveFile: (driveFileId: string) => void;
  /** Open on mount. Callers pass true when the list above came back empty. */
  startOpen: boolean;
  disabled?: boolean;
};

/**
 * The same game's backups that did not come from this world: backups of the
 * owner's other worlds, and archives whose world was deleted (reachable from
 * nowhere else). Another game's backups are excluded, since the restore would
 * be refused.
 *
 * Collapsed by default so the world's own backups stay the obvious choice;
 * callers open it when that list is empty.
 */
export default function DriveArchivePicker({
  otherBackups,
  otherFiles,
  loading,
  backupId,
  driveFileId,
  onPickBackup,
  onPickDriveFile,
  startOpen,
  disabled,
}: Props) {
  const [open, setOpen] = useState(startOpen);
  const orphans = otherFiles;
  const nothing = !loading && orphans.length === 0 && otherBackups.length === 0;

  return (
    <div className="drive-archives">
      <button
        type="button"
        className="accordion-toggle"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        {open ? "▾" : "▸"} Other backups in your Drive
      </button>

      {open && (
        <div className="accordion-body">
          {loading ? (
            <div className="muted small">Looking in your Drive…</div>
          ) : nothing ? (
            <div className="muted small">
              Nothing else found. Every archive in your Drive is already listed
              above.
            </div>
          ) : (
            <label>
              Backup to restore
              <select
                value={currentChoice(backupId, driveFileId)}
                disabled={disabled}
                onChange={(e) => {
                  const v = e.target.value;
                  if (v.startsWith("backup:"))
                    onPickBackup(v.slice("backup:".length));
                  else if (v.startsWith("drive:"))
                    onPickDriveFile(v.slice("drive:".length));
                  else {
                    onPickBackup("");
                    onPickDriveFile("");
                  }
                }}
              >
                <option value="">Select a backup…</option>
                {/* Ungrouped: the section heading already says these are
                    elsewhere in your Drive, so a group label over them only
                    repeats it. The deleted-worlds group below stays, because
                    nothing in those rows says the world is gone. */}
                {otherBackups.map((b) => (
                  <option key={b.id} value={`backup:${b.id}`}>
                    {backupLabel(b)}
                  </option>
                ))}
                {orphans.length > 0 && (
                  <optgroup label="From deleted worlds">
                    {orphans.map((f) => (
                      <option
                        key={f.drive_file_id}
                        value={`drive:${f.drive_file_id}`}
                      >
                        {driveLabel(f)}
                      </option>
                    ))}
                  </optgroup>
                )}
              </select>
            </label>
          )}
        </div>
      )}
    </div>
  );
}
