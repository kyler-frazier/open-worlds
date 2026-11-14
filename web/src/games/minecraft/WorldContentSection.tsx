import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api } from "../../api";
import type { AvailableContent, ContentKind, World } from "../../types";

interface Props {
  world: World;
  onClose: () => void;
}

// MODs need a loader, so they are hidden for vanilla; datapacks and resource
// packs apply to every server type.
function applicableKinds(world: World): ContentKind[] {
  const kinds: ContentKind[] = [];
  if (world.settings.server_type !== "VANILLA") kinds.push("MOD");
  kinds.push("DATAPACK", "RESOURCEPACK");
  return kinds;
}

const MULTI_KINDS: ContentKind[] = ["MOD", "DATAPACK"];

// Content changes answer 202 with a job id, which is followed to a terminal
// state so a failure is reported. The timeout covers a worker that never picks
// the job up.
const CONFIRM_POLL_MS = 500;
const CONFIRM_TIMEOUT_MS = 15000;

function setEq(a: Set<string>, b: Set<string>): boolean {
  if (a.size !== b.size) return false;
  for (const x of a) if (!b.has(x)) return false;
  return true;
}

// Selection is *staged* locally and only committed on "Save changes" — each
// add/remove enqueues a CONTENT worker job. We keep a `base*` snapshot of what's
// installed so we can diff on save and revert on cancel.
export default function WorldContentSection({ world, onClose }: Props) {
  const [lists, setLists] = useState<Record<string, AvailableContent[]>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Distinct from `busy`: the writes are done and we are only waiting to see
  // them land. Cancel stays available here, so a slow worker never strands you.
  const [confirming, setConfirming] = useState(false);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const [sel, setSel] = useState<Record<string, Set<string>>>({
    MOD: new Set(),
    DATAPACK: new Set(),
  });
  const [baseSel, setBaseSel] = useState<Record<string, Set<string>>>({
    MOD: new Set(),
    DATAPACK: new Set(),
  });
  const [rp, setRp] = useState("");
  const [baseRp, setBaseRp] = useState("");
  const [enforce, setEnforce] = useState(false);
  const [baseEnforce, setBaseEnforce] = useState(false);

  const offline = world.state === "OFF";
  const kinds = applicableKinds(world);

  const load = useCallback(async () => {
    setError(null);
    try {
      const out: Record<string, AvailableContent[]> = {};
      for (const k of applicableKinds(world)) {
        out[k] = await api.availableContent(world.id, k);
      }
      setLists(out);
      const mods = new Set(
        (out["MOD"] ?? []).filter((i) => i.installed).map((i) => i.catalog_id),
      );
      const dps = new Set(
        (out["DATAPACK"] ?? [])
          .filter((i) => i.installed)
          .map((i) => i.catalog_id),
      );
      const rpItem = (out["RESOURCEPACK"] ?? []).find((i) => i.installed);
      setSel({ MOD: new Set(mods), DATAPACK: new Set(dps) });
      setBaseSel({ MOD: new Set(mods), DATAPACK: new Set(dps) });
      setRp(rpItem?.catalog_id ?? "");
      setBaseRp(rpItem?.catalog_id ?? "");
      // The installed pack's current setting, so reopening the modal shows
      // the truth rather than an always-unchecked box.
      setEnforce(rpItem?.enforce ?? false);
      setBaseEnforce(rpItem?.enforce ?? false);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Failed to load content",
      );
    } finally {
      setLoading(false);
    }
  }, [world]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch on mount; state is set from the API response
    void load();
  }, [load]);

  const dirty = useMemo(() => {
    if (!setEq(sel.MOD, baseSel.MOD)) return true;
    if (!setEq(sel.DATAPACK, baseSel.DATAPACK)) return true;
    if (rp !== baseRp) return true;
    if (rp && enforce !== baseEnforce) return true;
    return false;
  }, [sel, baseSel, rp, baseRp, enforce, baseEnforce]);

  const toggle = (kind: ContentKind, id: string) => {
    setNote(null);
    setSel((prev) => {
      const next = new Set(prev[kind]);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return { ...prev, [kind]: next };
    });
  };

  const removeSel = (kind: ContentKind, id: string) => {
    setNote(null);
    setSel((prev) => {
      const next = new Set(prev[kind]);
      next.delete(id);
      return { ...prev, [kind]: next };
    });
  };

  /** Follow every queued job to a terminal state.
   *
   * Returns the errors of any that failed, so the caller can keep the panel
   * open and say what went wrong instead of closing on a silent failure.
   */
  const waitForJobs = async (jobIds: string[]): Promise<string[]> => {
    const pending = new Set(jobIds);
    const failures: string[] = [];
    const deadline = Date.now() + CONFIRM_TIMEOUT_MS;
    while (pending.size > 0 && Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, CONFIRM_POLL_MS));
      if (!alive.current) return [];
      for (const id of [...pending]) {
        try {
          const job = await api.jobStatus(world.id, id);
          if (job.status === "SUCCESS") pending.delete(id);
          else if (job.status === "FAILED") {
            pending.delete(id);
            failures.push(job.error || "Install failed.");
          }
        } catch {
          // One unreadable poll is not a failed job; try again next tick.
        }
      }
    }
    return failures;
  };

  const save = async () => {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      const jobs: string[] = [];
      for (const kind of MULTI_KINDS) {
        for (const id of sel[kind]) {
          if (!baseSel[kind].has(id))
            jobs.push(
              (await api.applyContent(world.id, { catalog_id: id })).job_id,
            );
        }
        for (const id of baseSel[kind]) {
          if (!sel[kind].has(id))
            jobs.push((await api.removeContent(world.id, id)).job_id);
        }
      }
      // A resource pack is single-select; applying a new one replaces the old
      // server-side, so we only need to remove when clearing it.
      if (rp !== baseRp || (rp && enforce !== baseEnforce)) {
        if (!rp) {
          if (baseRp)
            jobs.push((await api.removeContent(world.id, baseRp)).job_id);
        } else {
          jobs.push(
            (await api.applyContent(world.id, { catalog_id: rp, enforce }))
              .job_id,
          );
        }
      }
      setBaseSel({ MOD: new Set(sel.MOD), DATAPACK: new Set(sel.DATAPACK) });
      setBaseRp(rp);
      setBaseEnforce(enforce);
      setNote("Installing…");
      setBusy(false);
      setConfirming(true);
      const failures = await waitForJobs(jobs);
      if (!alive.current) return;
      if (failures.length > 0) {
        // Stay open: this is the case the job ids were added for. Reload so the
        // checkboxes show what actually landed rather than what was asked for.
        setNote(null);
        setError(failures.join(" "));
        await load();
        return;
      }
      onClose();
      return;
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Failed to queue changes",
      );
      await load();
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  };

  if (loading) return <div className="muted small">Loading content…</div>;

  const renderMultiAdd = (kind: ContentKind, label: string) => {
    const items = lists[kind] ?? [];
    const selected = sel[kind];
    const selectedItems = items.filter((i) => selected.has(i.catalog_id));
    return (
      <div className="content-block">
        <div className="field-label muted small">{label}</div>
        {items.length === 0 ? (
          <div className="muted small">
            No compatible {label.toLowerCase()} in the catalog for this world.
          </div>
        ) : (
          <>
            <details className="content-dropdown">
              <summary>
                Add {label.toLowerCase()}
                <span className="muted small"> · {items.length} available</span>
              </summary>
              <div className="content-dropdown-list">
                {items.map((it) => (
                  <label key={it.catalog_id} className="toggle-row">
                    <input
                      type="checkbox"
                      checked={selected.has(it.catalog_id)}
                      disabled={!offline}
                      onChange={() => toggle(kind, it.catalog_id)}
                    />
                    <span>{it.name}</span>
                    {it.server_side_only && (
                      <span
                        className="badge badge-server-side"
                        title="Works with a vanilla client — no client-side mod install required."
                      >
                        server-side only
                      </span>
                    )}
                  </label>
                ))}
              </div>
            </details>
            <div className="installed-list">
              {selectedItems.length === 0 ? (
                <div className="muted small">None selected.</div>
              ) : (
                selectedItems.map((it) => (
                  <div key={it.catalog_id} className="installed-row">
                    <span className="installed-name">{it.name}</span>
                    {it.server_side_only && (
                      <span className="badge badge-server-side">
                        server-side only
                      </span>
                    )}
                    <button
                      type="button"
                      className="btn-link-danger installed-remove"
                      disabled={!offline}
                      onClick={() => removeSel(kind, it.catalog_id)}
                    >
                      Remove
                    </button>
                  </div>
                ))
              )}
            </div>
          </>
        )}
      </div>
    );
  };

  const renderResourcePack = () => {
    const items = lists["RESOURCEPACK"] ?? [];
    return (
      <div className="content-block">
        <div className="field-label muted small">Resource pack</div>
        {items.length === 0 ? (
          <div className="muted small">
            No compatible resource packs in the catalog.
          </div>
        ) : (
          <>
            <select
              value={rp}
              disabled={!offline}
              onChange={(e) => {
                setNote(null);
                setRp(e.target.value);
              }}
            >
              <option value="">None</option>
              {items.map((it) => (
                <option key={it.catalog_id} value={it.catalog_id}>
                  {it.name}
                </option>
              ))}
            </select>
            <label className="toggle-row">
              <input
                type="checkbox"
                checked={enforce}
                disabled={!offline || !rp}
                onChange={(e) => {
                  setNote(null);
                  setEnforce(e.target.checked);
                }}
              />
              <span>Require clients to accept the pack</span>
            </label>
          </>
        )}
      </div>
    );
  };

  return (
    <div className="content-section">
      {!offline && (
        <div className="notice">
          Turn the world off to add or remove content.
        </div>
      )}
      {kinds.includes("MOD") && renderMultiAdd("MOD", "Mods")}
      {renderMultiAdd("DATAPACK", "Datapacks")}
      {renderResourcePack()}

      {note && <div className="muted small">{note}</div>}
      {error && <div className="error-banner">{error}</div>}

      <div className="modal-actions">
        <button
          type="button"
          className="btn btn-ghost"
          disabled={busy}
          onClick={onClose}
        >
          {confirming ? "Close" : "Cancel"}
        </button>
        <button
          type="button"
          className="btn btn-primary"
          disabled={busy || confirming || !dirty || !offline}
          onClick={() => void save()}
        >
          {busy ? "Saving…" : confirming ? "Installing…" : "Save changes"}
        </button>
      </div>
    </div>
  );
}
