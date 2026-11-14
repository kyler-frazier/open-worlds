import { useEffect, useState } from "react";
import { ApiError, api } from "../api";
import type { AdminGame } from "../types";

/**
 * Which games worlds may be created for. The API holds the rules -- at least
 * one game, never one that still has worlds, never one whose router is
 * missing -- and this only mirrors them, so the boxes it disables are the
 * changes the API would refuse anyway.
 */
export default function AdminGamesSection() {
  const [games, setGames] = useState<AdminGame[] | null>(null);
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);

  const hydrate = (list: AdminGame[]) => {
    setGames(list);
    setChosen(new Set(list.filter((g) => g.enabled).map((g) => g.game)));
  };

  useEffect(() => {
    api
      .adminListGames()
      .then(hydrate)
      .catch((err) =>
        setError(
          err instanceof ApiError ? err.message : "Failed to load games",
        ),
      );
  }, []);

  const toggle = (game: string) => {
    setSaved(false);
    const next = new Set(chosen);
    if (next.has(game)) next.delete(game);
    else next.add(game);
    setChosen(next);
  };

  const changed =
    games !== null && games.some((g) => g.enabled !== chosen.has(g.game));

  const save = async () => {
    setError(null);
    setSaving(true);
    try {
      hydrate(await api.adminSetGames([...chosen]));
      setSaved(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to save games");
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="admin-section">
      <h2>Games</h2>
      <p className="muted small">
        Which games people can create worlds for. A game with worlds stays on
        until they are deleted, and a game can only be turned on once its router
        runs on this host.
      </p>
      {games === null && !error ? (
        <div className="spinner" />
      ) : (
        <div className="settings-form">
          {(games ?? []).map((g) => {
            const on = chosen.has(g.game);
            // Turning on is blocked by a problem; turning off, by worlds or by
            // being the last one left.
            const lockedOn =
              g.enabled && (g.world_count > 0 || (on && chosen.size === 1));
            const blockedOff = !g.enabled && g.problems.length > 0;
            return (
              <div key={g.game} className="field">
                <label>
                  <input
                    type="checkbox"
                    checked={on}
                    disabled={lockedOn || blockedOff}
                    onChange={() => toggle(g.game)}
                  />{" "}
                  <strong>{g.display_name}</strong>{" "}
                  <span className="muted small">
                    {g.world_count} world{g.world_count === 1 ? "" : "s"}
                  </span>
                </label>
                {g.world_count > 0 && g.enabled && (
                  <span className="hint muted small">
                    Stays on while it has worlds.
                  </span>
                )}
                {g.problems.map((p) => (
                  <span
                    key={p}
                    className={`hint small ${g.enabled ? "error" : "muted"}`}
                  >
                    {p}
                  </span>
                ))}
              </div>
            );
          })}
          {error && <div className="error-banner">{error}</div>}
          {saved && <div className="muted small">Saved.</div>}
          <div>
            <button
              className="btn btn-primary"
              disabled={saving || !changed}
              onClick={() => void save()}
            >
              {saving ? "Saving…" : "Save games"}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
