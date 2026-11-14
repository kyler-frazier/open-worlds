import { useState } from "react";
import { ApiError, api } from "../api";
import { backupAndWait } from "../backup";
import type { PlatformUser, World } from "../types";

interface Props {
  user: PlatformUser;
  /** The worlds the API refused to revoke over. */
  worlds: World[];
  onClose: () => void;
  onRevoked: () => void;
}

type Mode = "delete" | "backup-delete";

/**
 * Revoke a user who still owns worlds: the API refuses a blind revoke, and the
 * admin decides here what happens to the worlds. Backups go to the user's own
 * Drive under their token, and every one must succeed before anything is
 * deleted.
 */
export default function RevokeUserDialog({
  user,
  worlds,
  onClose,
  onRevoked,
}: Props) {
  const [mode, setMode] = useState<Mode>(
    user.drive_linked ? "backup-delete" : "delete",
  );
  const [typed, setTyped] = useState("");
  const [progress, setProgress] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const working = progress !== null;

  const who = user.email ?? user.google_sub;
  const confirmWord = user.email ?? "revoke";
  const matchOk = typed.trim() === confirmWord;

  const proceed = async () => {
    setError(null);
    try {
      if (mode === "backup-delete") {
        for (const [i, w] of worlds.entries()) {
          setProgress(
            `Backing up "${w.display_name}" (${i + 1} of ${worlds.length})…`,
          );
          await backupAndWait(w.id);
        }
      }
      setProgress(
        `Deleting ${worlds.length} world${worlds.length === 1 ? "" : "s"} and revoking…`,
      );
      await api.adminRevokeUser(user.id, { deleteWorlds: true });
      onRevoked();
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : (err as Error).message);
      setProgress(null);
    }
  };

  return (
    <div className="modal-backdrop" onClick={() => !working && onClose()}>
      <div className="modal card" onClick={(e) => e.stopPropagation()}>
        <h2>Revoke {who}</h2>
        <p className="muted small">
          This user still owns {worlds.length} world
          {worlds.length === 1 ? "" : "s"}. Revoking removes their access; you
          decide what happens to the worlds.
        </p>

        <div className="table-scroll">
          <table className="data-table">
            <tbody>
              {worlds.map((w) => (
                <tr key={w.id}>
                  <td>{w.display_name}</td>
                  <td className="mono small muted">{w.connect_address}</td>
                  <td className="small muted">{w.state.toLowerCase()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="form">
          <label className="toggle-row">
            <input
              type="radio"
              name="revoke-mode"
              checked={mode === "backup-delete"}
              disabled={working || !user.drive_linked}
              onChange={() => setMode("backup-delete")}
            />
            <span>
              Back up each world to their Google Drive, then delete the worlds
              and revoke
              {!user.drive_linked && (
                <span className="muted small">
                  {" "}
                  (unavailable: they have not linked Drive)
                </span>
              )}
            </span>
          </label>
          <label className="toggle-row">
            <input
              type="radio"
              name="revoke-mode"
              checked={mode === "delete"}
              disabled={working}
              onChange={() => setMode("delete")}
            />
            <span>Delete the worlds and revoke, without a backup</span>
          </label>

          <label>
            To confirm, type <span className="mono">{confirmWord}</span>:
            <input
              autoFocus
              value={typed}
              disabled={working}
              onChange={(e) => setTyped(e.target.value)}
              placeholder={confirmWord}
            />
          </label>

          {error && <div className="error-banner">{error}</div>}
          {working && (
            <div className="inline-progress">
              <span className="spinner spinner-sm" />
              <span className="muted small">{progress}</span>
            </div>
          )}

          <div className="modal-actions">
            <button
              type="button"
              className="btn btn-ghost"
              disabled={working}
              onClick={onClose}
            >
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-danger"
              disabled={!matchOk || working}
              onClick={() => void proceed()}
            >
              {mode === "backup-delete"
                ? "Back up, delete & revoke"
                : "Delete & revoke"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
