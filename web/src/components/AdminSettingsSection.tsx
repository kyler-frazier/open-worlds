import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api";
import type { RuntimeSettings } from "../types";

const GIB = 1024 ** 3;

export default function AdminSettingsSection() {
  const [loaded, setLoaded] = useState(false);
  const [maxGib, setMaxGib] = useState("");
  const [fileCount, setFileCount] = useState("");
  const [names, setNames] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);

  const hydrate = useCallback((s: RuntimeSettings) => {
    setMaxGib(String(s.max_upload_bytes / GIB));
    setFileCount(String(s.max_upload_file_count));
    setNames(s.extra_allowed_root_names.join(", "));
    setLoaded(true);
  }, []);

  useEffect(() => {
    void (async () => {
      try {
        hydrate(await api.adminGetSettings());
      } catch (err) {
        setError(
          err instanceof ApiError ? err.message : "Failed to load settings",
        );
      }
    })();
  }, [hydrate]);

  const save = async () => {
    setError(null);
    setSaved(false);
    const gib = Number(maxGib);
    const count = Number(fileCount);
    if (!Number.isFinite(gib) || gib <= 0) {
      setError("Max upload size must be a positive number of GiB");
      return;
    }
    if (!Number.isInteger(count) || count < 1) {
      setError("Max file count must be a positive integer");
      return;
    }
    setSaving(true);
    try {
      const updated = await api.adminUpdateSettings({
        max_upload_bytes: Math.round(gib * GIB),
        max_upload_file_count: count,
        extra_allowed_root_names: names
          .split(",")
          .map((n) => n.trim())
          .filter(Boolean),
      });
      hydrate(updated);
      setSaved(true);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Failed to save settings",
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="admin-section">
      <h2>Platform settings</h2>
      {!loaded && !error ? (
        <div className="spinner" />
      ) : (
        <div className="settings-form">
          <label className="field">
            <span>Max world upload size (GiB)</span>
            <input
              type="number"
              min="0.1"
              step="0.1"
              value={maxGib}
              onChange={(e) => setMaxGib(e.target.value)}
            />
          </label>
          <label className="field">
            <span>Max files per upload</span>
            <input
              type="number"
              min="1"
              step="1"
              value={fileCount}
              onChange={(e) => setFileCount(e.target.value)}
            />
          </label>
          <label className="field">
            <span>Extra allowed top-level names (comma-separated)</span>
            <input
              type="text"
              value={names}
              placeholder="e.g. my-custom-dir, notes.txt"
              onChange={(e) => setNames(e.target.value)}
            />
          </label>
          {error && <div className="error-banner">{error}</div>}
          {saved && (
            <div className="muted small">Saved. Changes apply within ~30s.</div>
          )}
          <div>
            <button
              className="btn btn-primary"
              disabled={saving}
              onClick={() => void save()}
            >
              {saving ? "Saving…" : "Save settings"}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
