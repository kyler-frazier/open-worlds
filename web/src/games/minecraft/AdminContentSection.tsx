import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../../api";
import { confirmDialog } from "../../confirmService";
import type { CatalogEntry, ContentKind, ContentSource } from "../../types";

const KIND_TABS: { kind: ContentKind; label: string }[] = [
  { kind: "MOD", label: "Mods" },
  { kind: "DATAPACK", label: "Datapacks" },
  { kind: "RESOURCEPACK", label: "Resource packs" },
];

function csv(v: string): string[] {
  return v
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

export default function AdminContentSection() {
  const [kind, setKind] = useState<ContentKind>("MOD");
  const [entries, setEntries] = useState<CatalogEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Add form.
  const [source, setSource] = useState<ContentSource>("MODRINTH");
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [loaders, setLoaders] = useState("");
  const [gameVersions, setGameVersions] = useState("");
  const [url, setUrl] = useState("");
  const [sha512, setSha512] = useState("");
  const [sha1, setSha1] = useState("");
  const [adding, setAdding] = useState(false);

  const load = useCallback(async (k: ContentKind) => {
    setError(null);
    setEntries(null);
    try {
      setEntries(await api.adminListCatalog(k));
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Failed to load catalog",
      );
      setEntries([]);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch on mount; state is set from the API response
    void load(kind);
  }, [kind, load]);

  const resetForm = () => {
    setSlug("");
    setName("");
    setLoaders("");
    setGameVersions("");
    setUrl("");
    setSha512("");
    setSha1("");
  };

  const add = async () => {
    setError(null);
    if (!slug.trim()) {
      setError(
        source === "MODRINTH"
          ? "Enter a Modrinth slug."
          : "Enter an identifier.",
      );
      return;
    }
    setAdding(true);
    try {
      await api.adminAddCatalog({
        kind,
        source,
        project_slug: slug.trim(),
        ...(source === "PINNED"
          ? {
              name: name.trim(),
              loaders: csv(loaders),
              game_versions: csv(gameVersions),
              download_url: url.trim(),
              sha512: sha512.trim(),
              sha1: sha1.trim() || undefined,
            }
          : {}),
      });
      resetForm();
      await load(kind);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to add entry");
    } finally {
      setAdding(false);
    }
  };

  const toggle = async (e: CatalogEntry) => {
    try {
      await api.adminUpdateCatalog(e.id, { enabled: !e.enabled });
      await load(kind);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Failed to update entry",
      );
    }
  };

  const remove = async (e: CatalogEntry) => {
    const ok = await confirmDialog({
      title: "Remove from catalog",
      message: `Remove "${e.name}" from the ${kind.toLowerCase()} catalog? Worlds that installed it keep the files until re-imported.`,
      danger: true,
      confirmLabel: "Remove",
    });
    if (!ok) return;
    try {
      await api.adminDeleteCatalog(e.id);
      await load(kind);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Failed to remove entry",
      );
    }
  };

  return (
    <section className="admin-section">
      <h2>Add-on catalog</h2>
      <p className="muted small">
        Curate the mods, datapacks, and resource packs your users can install.
        Add by Modrinth slug (e.g. <code>fabric-api</code>); the correct file is
        resolved per world at install time.
      </p>

      <div className="source-tabs">
        {KIND_TABS.map((t) => (
          <button
            key={t.kind}
            type="button"
            className={`tab ${kind === t.kind ? "tab-active" : ""}`}
            onClick={() => setKind(t.kind)}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div className="settings-form">
        <label className="field">
          <span>Source</span>
          <select
            value={source}
            onChange={(e) => setSource(e.target.value as ContentSource)}
          >
            <option value="MODRINTH">Modrinth</option>
            <option value="PINNED">Pinned (direct URL)</option>
          </select>
        </label>
        <label className="field">
          <span>{source === "MODRINTH" ? "Modrinth slug" : "Identifier"}</span>
          <input
            value={slug}
            placeholder={
              source === "MODRINTH" ? "e.g. fabric-api" : "e.g. my-custom-pack"
            }
            onChange={(e) => setSlug(e.target.value)}
          />
        </label>
        {source === "PINNED" && (
          <>
            <label className="field">
              <span>Display name</span>
              <input value={name} onChange={(e) => setName(e.target.value)} />
            </label>
            <label className="field">
              <span>Loaders (comma-separated)</span>
              <input
                value={loaders}
                placeholder={
                  kind === "DATAPACK"
                    ? "datapack"
                    : kind === "RESOURCEPACK"
                      ? "minecraft"
                      : "e.g. fabric, forge"
                }
                onChange={(e) => setLoaders(e.target.value)}
              />
            </label>
            <label className="field">
              <span>Game versions (comma-separated)</span>
              <input
                value={gameVersions}
                placeholder="e.g. 1.20.4, 1.20.6"
                onChange={(e) => setGameVersions(e.target.value)}
              />
            </label>
            <label className="field">
              <span>Download URL</span>
              <input value={url} onChange={(e) => setUrl(e.target.value)} />
            </label>
            <label className="field">
              <span>sha512</span>
              <input
                value={sha512}
                onChange={(e) => setSha512(e.target.value)}
              />
            </label>
            <label className="field">
              <span>
                sha1 {kind === "RESOURCEPACK" ? "(recommended)" : "(optional)"}
              </span>
              <input value={sha1} onChange={(e) => setSha1(e.target.value)} />
            </label>
          </>
        )}
        {error && <div className="error-banner">{error}</div>}
        <div>
          <button
            className="btn btn-primary"
            disabled={adding}
            onClick={() => void add()}
          >
            {adding ? "Adding…" : "Add to catalog"}
          </button>
        </div>
      </div>

      {entries === null ? (
        <div className="spinner" />
      ) : entries.length === 0 ? (
        <div className="muted small">No {kind.toLowerCase()} entries yet.</div>
      ) : (
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Source</th>
                <th>Loaders</th>
                <th>Enabled</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e) => (
                <tr key={e.id}>
                  <td>
                    {e.name}
                    <span className="mono small muted">
                      {" "}
                      · {e.project_slug}
                    </span>
                  </td>
                  <td>{e.source}</td>
                  <td className="small muted">{e.loaders.join(", ") || "—"}</td>
                  <td>
                    <label className="toggle-row">
                      <input
                        type="checkbox"
                        checked={e.enabled}
                        onChange={() => void toggle(e)}
                      />
                    </label>
                  </td>
                  <td>
                    <button
                      className="btn btn-ghost btn-sm"
                      onClick={() => void remove(e)}
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
