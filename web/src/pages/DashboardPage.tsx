import { useCallback, useMemo, useState } from "react";
import { useNavigate } from "react-router";
import { api } from "../api";
import { isStrictOwner } from "../access";
import { useAuth } from "../auth";
import CreateWorldModal from "../components/CreateWorldModal";
import ErrorNotifications from "../components/ErrorNotifications";
import Header from "../components/Header";
import WorldCard from "../components/WorldCard";
import WorldSortSelect, { useWorldSort } from "../components/WorldSortSelect";
import { sortWorlds, withUpdatedWorld } from "../worldList";
import type { World } from "../types";
import { usePolling } from "../usePolling";

// Refresh cadence. The fast one is matched to the backend's readiness probe --
// polling faster than the signal can change would just add requests.
const IDLE_POLL_MS = 10000;
const FAST_POLL_MS = 3000;

export default function DashboardPage() {
  const { me } = useAuth();
  const navigate = useNavigate();
  const [worlds, setWorlds] = useState<World[] | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [sort, setSort] = useWorldSort();
  const sorted = useMemo(
    () => (worlds ? sortWorlds(worlds, sort) : null),
    [worlds, sort],
  );

  const load = useCallback(async () => {
    setWorlds(await api.listWorlds());
  }, []);

  // A world mid-boot or mid-shutdown is where the page is worth refreshing
  // quickly: the user is watching for it to become joinable, or for the stop
  // they asked for to land. Everything else (auto-sleep, player counts) is fine
  // at the relaxed rate, so the fast poll costs nothing at steady state.
  const anyTransitioning =
    worlds?.some((w) => w.readiness === "STARTING" || w.state === "STOPPING") ??
    false;
  const interval = anyTransitioning ? FAST_POLL_MS : IDLE_POLL_MS;

  // Paused while the tab is hidden, and refreshed the moment it comes back --
  // see usePolling. The skip keeps a tick from re-rendering an open modal
  // underneath the user mid-edit.
  usePolling(load, interval, {
    skip: () => document.querySelector(".modal-backdrop") !== null,
  });

  // The create prompt keys off worlds you OWN. Someone whose only worlds
  // were shared with them has nothing of their own and should still be
  // invited to make one.
  const ownedCount = (worlds ?? []).filter((w) => isStrictOwner(w, me)).length;

  return (
    <div className="page">
      <Header />
      <main className="content">
        <div className="content-head">
          <h1>Worlds</h1>
          <div className="content-head-actions">
            {/* Moved down out of the app header, which was crowded enough on a
                phone that the account menu had nowhere to sit. */}
            {me?.is_admin && (
              <button
                className="btn btn-secondary"
                onClick={() => navigate("/admin")}
              >
                Admin
              </button>
            )}
            <button
              className="btn btn-primary"
              onClick={() => setShowCreate(true)}
            >
              + New world
            </button>
          </div>
        </div>

        {/* A world with auto-backup on but no Drive link means the link was
            lost -- the worker unlinks when Google reports the token revoked --
            so say that, not the generic first-time prompt. */}
        {!me?.drive_linked &&
          (worlds?.some((w) => w.auto_backup_enabled) ? (
            <div className="notice notice-warn">
              Google Drive access was lost, so automatic backups are paused.{" "}
              <a href={api.driveLinkUrl()}>Re-link Drive to resume →</a>
            </div>
          ) : (
            <div className="notice">
              Link your Google Drive to enable backups.{" "}
              <a href={api.driveLinkUrl()}>Link now →</a>
            </div>
          ))}

        {worlds === null ? (
          <div className="spinner" />
        ) : ownedCount === 0 && worlds.length === 0 ? (
          <div className="empty card">
            <p>You don't have any worlds yet.</p>
            <button
              className="btn btn-primary"
              onClick={() => setShowCreate(true)}
            >
              Create your first world
            </button>
          </div>
        ) : (
          <>
            <div className="list-toolbar">
              <WorldSortSelect value={sort} onChange={setSort} />
            </div>
            <div className="grid">
              {(sorted ?? []).map((w) => (
                <WorldCard
                  key={w.id}
                  world={w}
                  driveLinked={!!me?.drive_linked}
                  onChanged={load}
                  onUpdated={(u) => setWorlds((ws) => withUpdatedWorld(ws, u))}
                />
              ))}
            </div>
          </>
        )}
      </main>

      <ErrorNotifications worlds={worlds ?? []} />

      {showCreate && (
        <CreateWorldModal
          driveLinked={!!me?.drive_linked}
          onClose={() => setShowCreate(false)}
          onCreated={load}
        />
      )}
    </div>
  );
}
