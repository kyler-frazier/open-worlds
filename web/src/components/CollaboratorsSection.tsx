import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api";
import { confirmDialog } from "../confirmService";
import type { Collaborator, CollaboratorLevel, World } from "../types";

interface Props {
  world: World;
  onChanged: () => void;
  onClose: () => void;
}

const LEVELS: { value: CollaboratorLevel; label: string; hint: string }[] = [
  {
    value: "OPERATOR",
    label: "Operator",
    hint: "Turn the world on, off and wake it. Nothing else.",
  },
  {
    value: "MANAGER",
    label: "Manager",
    hint: "The above, plus settings like memory, version and player cap.",
  },
];

/**
 * Who else can act on this world.
 *
 * Only rendered for the owner. Admins are deliberately excluded from granting
 * access, matching the server: managing someone's world is operational,
 * handing a third party a standing key to it is not.
 */
export default function CollaboratorsSection({
  world,
  onChanged,
  onClose,
}: Props) {
  const [rows, setRows] = useState<Collaborator[] | null>(null);
  const [email, setEmail] = useState("");
  const [level, setLevel] = useState<CollaboratorLevel>("OPERATOR");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setRows(await api.listCollaborators(world.id));
    } catch {
      setRows([]);
    }
  }, [world.id]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch on mount; state is set from the API response
    void refresh();
  }, [refresh]);

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.addCollaborator(world.id, email.trim(), level);
      setEmail("");
      await refresh();
      onChanged();
    } catch (err) {
      // The server's messages here are written to be read by the person who
      // hit them -- an unknown email explains that they must sign in first --
      // so show it rather than a generic failure.
      setError(
        err instanceof ApiError ? err.message : "Could not add that person",
      );
    } finally {
      setBusy(false);
    }
  };

  const changeLevel = async (c: Collaborator, next: CollaboratorLevel) => {
    setError(null);
    try {
      await api.updateCollaborator(world.id, c.user_id, next);
      await refresh();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not change access",
      );
    }
  };

  const remove = async (c: Collaborator) => {
    const who = c.email ?? c.display_name ?? "this person";
    const ok = await confirmDialog({
      title: "Remove access",
      message: `Remove ${who}'s access to "${world.display_name}"? They will no longer see it at all.`,
      danger: true,
      confirmLabel: "Remove",
    });
    if (!ok) return;
    setError(null);
    try {
      await api.removeCollaborator(world.id, c.user_id);
      await refresh();
      onChanged();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not remove access",
      );
    }
  };

  return (
    <div className="form">
      <div className="muted small">
        People you add here can act on <strong>{world.display_name}</strong>{" "}
        without being able to see any of your other worlds. Backups stay yours:
        nobody else can create, delete or restore them, because they live in
        your Google Drive.
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="backups-scroll">
        {rows === null ? (
          <div className="muted small">Loading…</div>
        ) : rows.length === 0 ? (
          <div className="muted small">
            Nobody else has access to this world.
          </div>
        ) : (
          <table className="backups-table">
            <tbody>
              {rows.map((c) => (
                <tr key={c.user_id}>
                  <td>{c.email ?? c.display_name ?? c.user_id}</td>
                  <td>
                    <select
                      value={c.level}
                      aria-label={`Access level for ${c.email ?? c.user_id}`}
                      onChange={(e) =>
                        void changeLevel(c, e.target.value as CollaboratorLevel)
                      }
                    >
                      {LEVELS.map((l) => (
                        <option key={l.value} value={l.value}>
                          {l.label}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td className="backup-actions">
                    <button
                      className="btn-link-danger"
                      onClick={() => void remove(c)}
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <hr className="modal-divider" />

      <form onSubmit={(e) => void add(e)}>
        <div className="form-row form-row-2">
          <label>
            Their email
            <input
              type="email"
              required
              placeholder="friend@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </label>
          <label>
            Access
            <select
              value={level}
              onChange={(e) => setLevel(e.target.value as CollaboratorLevel)}
            >
              {LEVELS.map((l) => (
                <option key={l.value} value={l.value}>
                  {l.label}
                </option>
              ))}
            </select>
          </label>
        </div>
        <span className="hint muted">
          {LEVELS.find((l) => l.value === level)?.hint} They must already have
          an account here and have signed in at least once.
        </span>
        <div className="modal-actions">
          <button
            type="button"
            className="btn btn-ghost"
            disabled={busy}
            onClick={onClose}
          >
            Cancel
          </button>
          <button
            type="submit"
            className="btn btn-primary"
            disabled={busy || !email.trim()}
          >
            {busy ? "Adding…" : "Add person"}
          </button>
        </div>
      </form>
    </div>
  );
}
