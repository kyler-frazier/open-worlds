import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api";
import { confirmDialog } from "../confirmService";
import type { Backup, World } from "../types";

interface Props {
  world: World;
  driveLinked: boolean;
  onChanged: () => void;
  /** Whether the viewer may create and delete. False for collaborators: the
   *  archives live in the OWNER's Drive, and nothing in the backup path caps
   *  how many a caller may queue. Reading the list stays open to them. */
  canModify: boolean;
}

function formatBytes(n: number): string {
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(1)} ${units[i]}`;
}

/**
 * The backup list and its actions, without modal chrome. A tab in Edit, beside
 * the automatic-backup settings, so one view answers "is this world being
 * backed up".
 */
export default function BackupsSection({
  world,
  driveLinked,
  onChanged,
  canModify,
}: Props) {
  const [backups, setBackups] = useState<Backup[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setBackups(await api.listBackups(world.id));
    } catch {
      setBackups([]);
    }
  }, [world.id]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch on mount; state is set from the API response
    void refresh();
  }, [refresh]);

  // Poll while a backup is still in flight so its status advances without a
  // manual refresh.
  useEffect(() => {
    if (!backups) return;
    const pending = backups.some(
      (b) => b.status === "PENDING" || b.status === "RUNNING",
    );
    if (!pending) return;
    const timer = setInterval(() => void refresh(), 4000);
    return () => clearInterval(timer);
  }, [backups, refresh]);

  const backUp = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.createBackup(world.id);
      onChanged();
      await refresh();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not start backup",
      );
    } finally {
      setBusy(false);
    }
  };

  const deleteBackup = async (b: Backup) => {
    const ok = await confirmDialog({
      title: "Delete backup",
      message: `Delete backup "${b.filename}"? This removes it from Google Drive.`,
      danger: true,
      confirmLabel: "Delete",
    });
    if (!ok) return;
    setError(null);
    try {
      await api.deleteBackup(world.id, b.id);
      await refresh();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not delete backup",
      );
    }
  };

  return (
    <>
      <div className="muted small backups-caption">
        Each backup snapshots your world's save data and configuration — saved
        to your Google Drive as a <code>.tar.gz</code> under the{" "}
        <a
          href="https://drive.google.com/drive/search?q=title:%22Open%20Worlds%20Backups%22%20type:folder"
          target="_blank"
          rel="noopener noreferrer"
        >
          Open Worlds Backups
        </a>{" "}
        folder (this world's backups are in the{" "}
        <code>
          {world.game}/{world.subdomain}
        </code>{" "}
        subfolder). The game install itself and files the server regenerates on
        startup are left out, to keep backups small.
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="backups-scroll">
        {backups === null ? (
          <div className="muted small">Loading…</div>
        ) : backups.length === 0 ? (
          <div className="muted small">No backups yet.</div>
        ) : (
          <table className="backups-table">
            <tbody>
              {backups.map((b) => (
                <tr key={b.id}>
                  <td className="mono">{b.filename}</td>
                  <td>{b.kind}</td>
                  <td
                    className={
                      b.status === "FAILED" ? "backup-failed" : undefined
                    }
                    title={
                      b.status === "FAILED" && b.error ? b.error : undefined
                    }
                  >
                    {b.status}
                    {b.status === "FAILED" && b.error ? " ⓘ" : ""}
                  </td>
                  <td>{b.size_bytes ? formatBytes(b.size_bytes) : "—"}</td>
                  <td className="backup-actions">
                    {canModify && (
                      <button
                        className="btn-link-danger"
                        title="Delete this backup from Google Drive"
                        disabled={
                          b.status === "PENDING" || b.status === "RUNNING"
                        }
                        onClick={() => void deleteBackup(b)}
                      >
                        Delete
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="backups-bar">
        {canModify && (
          <button
            className="btn btn-secondary"
            disabled={busy || !driveLinked}
            title={
              driveLinked
                ? "Back up to Google Drive"
                : "Link Google Drive first"
            }
            onClick={() => void backUp()}
          >
            {busy ? "Queued…" : "Back up now"}
          </button>
        )}
      </div>
    </>
  );
}
