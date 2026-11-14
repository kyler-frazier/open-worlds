import { useEffect, useMemo, useState } from "react";
import type { World } from "../types";
import { errorKey, loadSeen, openWorldError, saveSeen } from "../worldErrors";

const MAX_VISIBLE = 3;

/**
 * Pop-in notifications, bottom right, for a world's server error the user has
 * not seen yet -- including one that happened while the page was closed. They
 * stay until dismissed or opened: an error is worth acknowledging, and one
 * that vanished on a timer could be missed entirely.
 */
export default function ErrorNotifications({ worlds }: { worlds: World[] }) {
  const [seen, setSeen] = useState<Set<string>>(() => loadSeen());

  const current = useMemo(
    () => worlds.filter((w) => w.last_error && w.last_error_at),
    [worlds],
  );
  const unseen = current.filter((w) => !seen.has(errorKey(w)));

  // Keep the stored set to errors that still exist, once the list has loaded.
  useEffect(() => {
    if (worlds.length) saveSeen(seen, current.map(errorKey));
  }, [seen, current, worlds.length]);

  const acknowledge = (w: World) =>
    setSeen((prev) => new Set(prev).add(errorKey(w)));

  if (unseen.length === 0) return null;
  const visible = unseen.slice(0, MAX_VISIBLE);
  const hidden = unseen.length - visible.length;

  return (
    <div className="toasts" role="region" aria-label="Notifications">
      {visible.map((w) => (
        <div key={errorKey(w)} className="toast toast-error" role="alert">
          <div className="toast-head">
            <strong className="toast-title">
              {w.display_name}{" "}
              {w.state === "ERROR" ? "failed to start" : "isn't responding"}
            </strong>
            <button
              className="toast-close"
              aria-label="Dismiss"
              title="Dismiss"
              onClick={() => acknowledge(w)}
            >
              ×
            </button>
          </div>
          {w.last_error_summary && (
            <div className="toast-body">{w.last_error_summary}</div>
          )}
          <div className="toast-actions">
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => {
                acknowledge(w);
                openWorldError(w.id);
              }}
            >
              Show details
            </button>
          </div>
        </div>
      ))}
      {hidden > 0 && (
        <div className="toast toast-more muted small">
          +{hidden} more {hidden === 1 ? "error" : "errors"}
        </div>
      )}
    </div>
  );
}
