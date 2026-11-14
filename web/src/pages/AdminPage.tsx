import { useCallback, useMemo, useState, type FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router";
import { ApiError, api } from "../api";
import { confirmDialog } from "../confirmService";
import { gameAdminSections } from "../games";
import AdminMetricsSection from "../components/AdminMetricsSection";
import AdminGamesSection from "../components/AdminGamesSection";
import AdminSettingsSection from "../components/AdminSettingsSection";
import Header from "../components/Header";
import RevokeUserDialog from "../components/RevokeUserDialog";
import WorldCard from "../components/WorldCard";
import WorldSortSelect, { useWorldSort } from "../components/WorldSortSelect";
import { sortWorlds, withUpdatedWorld } from "../worldList";
import type { PlatformUser, World } from "../types";
import { usePolling } from "../usePolling";

/** A 409 from revoke carries the worlds standing in the way. */
function worldsBlockingRevoke(err: unknown): World[] | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null;
  const d = err.detail as { worlds?: World[] } | undefined;
  return Array.isArray(d?.worlds) ? d.worlds : null;
}

// Matches the dashboard's idle rate. Nothing on this page changes faster than
// a world's state does, and the worker's sweep is the slowest input anyway.
const ADMIN_POLL_MS = 10000;

// One tab per section, including any a game brings (an add-on catalog). The
// choice lives in the URL, so a reload or a link lands on the same tab.
const GAME_SECTIONS = gameAdminSections();
const TABS: { id: string; label: string }[] = [
  { id: "users", label: "Allow-list" },
  { id: "settings", label: "Platform settings" },
  ...GAME_SECTIONS.map(({ id, label }) => ({ id, label })),
  { id: "worlds", label: "All worlds" },
  { id: "metrics", label: "Metrics" },
];
type TabId = string;

function useAdminTab(): [TabId, (t: TabId) => void] {
  const [params, setParams] = useSearchParams();
  const raw = params.get("tab");
  const tab = TABS.find((t) => t.id === raw)?.id ?? "users";
  const setTab = useCallback(
    (t: TabId) => setParams({ tab: t }, { replace: true }),
    [setParams],
  );
  return [tab, setTab];
}

export default function AdminPage() {
  const [users, setUsers] = useState<PlatformUser[] | null>(null);
  const [worlds, setWorlds] = useState<World[] | null>(null);
  const [newSub, setNewSub] = useState("");
  const [addError, setAddError] = useState<string | null>(null);
  const [ownerFilter, setOwnerFilter] = useState<string>("ALL");
  const [revoking, setRevoking] = useState<{
    user: PlatformUser;
    worlds: World[];
  } | null>(null);
  const [revokeError, setRevokeError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const navigate = useNavigate();
  const [tab, setTab] = useAdminTab();

  const loadUsers = useCallback(
    async () => setUsers(await api.adminListUsers()),
    [],
  );
  const loadWorlds = useCallback(
    async () => setWorlds(await api.adminListWorlds()),
    [],
  );

  // Polled at the dashboard's idle rate; an admin watches the same transitions.
  const load = useCallback(async () => {
    await Promise.all([loadUsers(), loadWorlds()]);
  }, [loadUsers, loadWorlds]);

  usePolling(load, ADMIN_POLL_MS, {
    // A revoke dialog is a multi-step decision over a list of worlds; a refresh
    // underneath it would swap the list the operator is reading.
    skip: () =>
      document.querySelector(".modal-backdrop") !== null || revoking !== null,
  });

  const addUser = async (e: FormEvent) => {
    e.preventDefault();
    setAddError(null);
    try {
      await api.adminAddUser(newSub.trim());
      setNewSub("");
      await loadUsers();
    } catch (err) {
      setAddError(err instanceof ApiError ? err.message : "Failed to add user");
    }
  };

  const owners = useMemo(() => {
    const map = new Map<string, string>();
    (worlds ?? []).forEach((w) => {
      if (w.owner_id) map.set(w.owner_id, w.owner_email ?? w.owner_id);
    });
    return Array.from(map.entries());
  }, [worlds]);

  // Shares the dashboard's remembered choice: one preference for how worlds
  // are listed, wherever they are.
  const [sort, setSort] = useWorldSort();
  const filteredWorlds = sortWorlds(
    (worlds ?? []).filter(
      (w) => ownerFilter === "ALL" || w.owner_id === ownerFilter,
    ),
    sort,
  );

  return (
    <div className="page">
      <Header />
      <main className="content">
        <div className="content-head">
          <h1>Admin</h1>
          <button className="btn btn-secondary" onClick={() => navigate("/")}>
            Return to My Worlds
          </button>
        </div>

        <div className="tabs" role="tablist" aria-label="Admin sections">
          {TABS.map((t) => (
            <button
              key={t.id}
              role="tab"
              aria-selected={tab === t.id}
              className={`tab${tab === t.id ? " tab-active" : ""}`}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </div>

        {tab === "users" && (
          <section className="admin-section">
            <h2>Allow-list</h2>
            <form onSubmit={addUser} className="add-user-form">
              <input
                value={newSub}
                onChange={(e) => setNewSub(e.target.value)}
                placeholder="Paste a Google sub to approve"
              />
              <button className="btn btn-primary" type="submit">
                Approve
              </button>
            </form>
            {addError && <div className="error-banner">{addError}</div>}
            {revokeError && <div className="error-banner">{revokeError}</div>}
            {notice && <div className="notice">{notice}</div>}

            {users === null ? (
              <div className="spinner" />
            ) : (
              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Email</th>
                      <th>Google sub</th>
                      <th>Role</th>
                      <th>Drive</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {users.map((u) => (
                      <tr key={u.id}>
                        <td>{u.email ?? u.display_name ?? "—"}</td>
                        <td className="mono small">{u.google_sub}</td>
                        <td>{u.role}</td>
                        <td>{u.drive_linked ? "linked" : "—"}</td>
                        <td className="row-actions">
                          {/* The lever between "do nothing" and "revoke": ends the
                            user's sessions everywhere; the account and worlds
                            are untouched and they can sign straight back in. */}
                          <button
                            className="btn btn-ghost btn-sm"
                            title="End this user's sessions in every browser; their account is untouched"
                            onClick={async () => {
                              const ok = await confirmDialog({
                                title: "Sign out everywhere",
                                message: `Sign ${u.email ?? u.google_sub} out of every browser? Their account and worlds are untouched; they can sign back in at any time.`,
                                confirmLabel: "Sign out",
                              });
                              if (!ok) return;
                              setNotice(null);
                              setRevokeError(null);
                              try {
                                await api.adminForceLogout(u.id);
                                setNotice(
                                  `${u.email ?? u.google_sub} has been signed out everywhere.`,
                                );
                              } catch (err) {
                                setRevokeError(
                                  err instanceof ApiError
                                    ? err.message
                                    : "Failed to sign out user",
                                );
                              }
                            }}
                          >
                            Sign out
                          </button>
                          <button
                            className="btn btn-ghost btn-sm"
                            onClick={async () => {
                              const ok = await confirmDialog({
                                title: "Revoke access",
                                message: `Revoke access for ${u.email ?? u.google_sub}?`,
                                danger: true,
                                confirmLabel: "Revoke",
                              });
                              if (!ok) return;
                              setRevokeError(null);
                              try {
                                await api.adminRevokeUser(u.id);
                                await loadUsers();
                              } catch (err) {
                                // The API refuses when the user still owns worlds;
                                // hand the decision about those to the dialog.
                                const blocking = worldsBlockingRevoke(err);
                                if (blocking)
                                  setRevoking({ user: u, worlds: blocking });
                                else
                                  setRevokeError(
                                    err instanceof ApiError
                                      ? err.message
                                      : "Failed to revoke user",
                                  );
                              }
                            }}
                          >
                            Revoke
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        )}

        {tab === "settings" && (
          <>
            <AdminGamesSection />
            <AdminSettingsSection />
          </>
        )}

        {GAME_SECTIONS.map(
          ({ id, Component }) => tab === id && <Component key={id} />,
        )}

        {tab === "worlds" && (
          <section className="admin-section">
            <div className="content-head">
              <h2>All worlds</h2>
              <div className="content-head-actions">
                <WorldSortSelect value={sort} onChange={setSort} />
                <select
                  value={ownerFilter}
                  onChange={(e) => setOwnerFilter(e.target.value)}
                >
                  <option value="ALL">All owners</option>
                  {owners.map(([id, label]) => (
                    <option key={id} value={id}>
                      {label}
                    </option>
                  ))}
                </select>
              </div>
            </div>
            {worlds === null ? (
              <div className="spinner" />
            ) : (
              <div className="grid">
                {filteredWorlds.map((w) => (
                  <WorldCard
                    key={w.id}
                    world={w}
                    driveLinked
                    showOwner
                    onChanged={loadWorlds}
                    onUpdated={(u) =>
                      setWorlds((ws) => withUpdatedWorld(ws, u))
                    }
                  />
                ))}
              </div>
            )}
          </section>
        )}

        {tab === "metrics" && <AdminMetricsSection />}
      </main>

      {revoking && (
        <RevokeUserDialog
          user={revoking.user}
          worlds={revoking.worlds}
          onClose={() => setRevoking(null)}
          onRevoked={() => {
            void loadUsers();
            void loadWorlds();
          }}
        />
      )}
    </div>
  );
}
