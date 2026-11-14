import { useEffect, useState } from "react";
import { ApiError } from "../api";
import { backupAndWait } from "../backup";
import {
  registerConfirm,
  setConfirmOpen,
  type ConfirmOptions,
} from "../confirmService";

interface Pending extends ConfirmOptions {
  resolve: (ok: boolean) => void;
}

/** Mounted once at the app root; renders confirmDialog() requests as a modal. */
export default function ConfirmHost() {
  const [pending, setPending] = useState<Pending | null>(null);
  const [typed, setTyped] = useState("");
  const [backupFirst, setBackupFirst] = useState(false);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    registerConfirm(
      (options) =>
        new Promise<boolean>((resolve) => {
          setTyped("");
          setBackupFirst(!!options.backup?.driveLinked);
          setWorking(false);
          setError(null);
          setPending({ ...options, resolve });
          setConfirmOpen(true);
        }),
    );
  }, []);

  if (!pending) return null;

  const finish = (ok: boolean) => {
    pending.resolve(ok);
    setPending(null);
    setConfirmOpen(false);
    setTyped("");
    setWorking(false);
    setError(null);
  };

  const onConfirm = async () => {
    if (pending.backup && backupFirst && pending.backup.driveLinked) {
      setError(null);
      setWorking(true);
      try {
        await backupAndWait(pending.backup.worldId);
      } catch (err) {
        setError(
          err instanceof ApiError ? err.message : (err as Error).message,
        );
        setWorking(false);
        return;
      }
    }
    finish(true);
  };

  const matchOk = !pending.requireText || typed.trim() === pending.requireText;

  return (
    <div className="modal-backdrop" onClick={() => !working && finish(false)}>
      <div className="modal card modal-sm" onClick={(e) => e.stopPropagation()}>
        {pending.title && <h2>{pending.title}</h2>}
        <p className="confirm-message">{pending.message}</p>
        {pending.details}

        {pending.requireText && (
          <label>
            To confirm, type your subdomain exactly:
            <div className="mono confirm-subdomain">{pending.requireText}</div>
            <input
              autoFocus
              value={typed}
              disabled={working}
              onChange={(e) => setTyped(e.target.value)}
              placeholder={pending.requireText}
            />
          </label>
        )}

        {pending.backup && (
          <>
            <label className="toggle-row">
              <input
                type="checkbox"
                checked={backupFirst}
                disabled={working || !pending.backup.driveLinked}
                onChange={(e) => setBackupFirst(e.target.checked)}
              />
              <span>Create a backup of this game before proceeding</span>
            </label>
            {!pending.backup.driveLinked && (
              <span className="hint muted">
                Link Google Drive to back up first.
              </span>
            )}
          </>
        )}

        {error && <div className="error-banner">{error}</div>}
        {working && (
          <div className="inline-progress">
            <span className="spinner spinner-sm" />
            <span className="muted small">
              Backing up… this can take a moment.
            </span>
          </div>
        )}

        <div className="modal-actions">
          <button
            type="button"
            className="btn btn-ghost"
            disabled={working}
            onClick={() => finish(false)}
          >
            {pending.cancelLabel ?? "Cancel"}
          </button>
          <button
            type="button"
            className={`btn ${pending.danger ? "btn-danger" : "btn-primary"}`}
            disabled={!matchOk || working}
            onClick={() => void onConfirm()}
          >
            {pending.confirmLabel ?? "Confirm"}
          </button>
        </div>
      </div>
    </div>
  );
}
